# Copyright 2022 Cerebras Systems.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import random
from abc import abstractmethod

import torch
from torchvision import transforms

from modelzoo.common.pytorch import cb_model as cm
from modelzoo.common.pytorch.input_utils import get_streaming_batch_size
from modelzoo.vision.pytorch.input.utils import (
    FastDataLoader,
    create_worker_cache,
    num_tasks,
    task_id,
)
from modelzoo.vision.pytorch.unet.input.preprocessing_utils import (
    normalize_tensor_transform,
)


class Hdf5BaseDataProcessor(torch.utils.data.Dataset):
    """
    A HDF5 dataset processor for UNet HDF dataset.
    Performs on-the-fly augmentation of image and labek.

    Functionality includes:
        Reading data from HDF5 documents
        Augmenting data

    :param dict params: dict containing training
        input parameters for creating dataset.
    Expects the following fields:

    - "data_dir" (str or list of str): Path to dataset HDF5 files
    - "num_classes (int): Maximum length of the sequence to generate
    - "image_shape" (int): Expected shape of output images and label, used in assert checks.
    - "loss" (str): Loss type, supported: {"bce", "multilabel_bce", "ssce"}
    - "normalize_data_method" (str): Can be one of {None, "zero_centered", "zero_one"}
    - "batch_size" (int): Batch size.
    - "shuffle" (bool): Flag to enable data shuffling.
    - "shuffle_buffer" (int): Size of shuffle buffer in samples.
    - "shuffle_seed" (int): Shuffle seed.
    - "num_workers" (int):  How many subprocesses to use for data loading.
    - "drop_last" (bool): If True and the dataset size is not divisible
       by the batch size, the last incomplete batch will be dropped.
    - "prefetch_factor" (int): Number of samples loaded in advance by each worker.
    - "persistent_workers" (bool): If True, the data loader will not shutdown
       the worker processes after a dataset has been consumed once.
    """

    def __init__(self, params):
        super(Hdf5BaseDataProcessor, self).__init__()

        use_worker_cache = params["use_worker_cache"]
        self.data_dir = params["data_dir"]
        if use_worker_cache and cm.is_streamer():
            if not cm.is_appliance():
                raise RuntimeError(
                    "use_worker_cache not supported for non-appliance runs"
                )
            else:
                self.data_dir = create_worker_cache(self.data_dir)

        self.num_classes = params["num_classes"]
        self.normalize_data_method = params.get("normalize_data_method")
        if self.normalize_data_method:
            # Normalize
            self.normalize_transform = transforms.Lambda(
                self._apply_normalization
            )

        self.image_shape = params["image_shape"]  # of format (H, W, C)
        (
            self.tgt_image_height,
            self.tgt_image_width,
            self.channels,
        ) = self.image_shape

        self.loss_type = params["loss"]

        self.shuffle_seed = params.get("shuffle_seed", None)
        if self.shuffle_seed:
            torch.manual_seed(self.shuffle_seed)

        self.augment_data = params.get("augment_data", True)
        self.batch_size = get_streaming_batch_size(params["batch_size"])
        self.shuffle = params.get("shuffle", True)

        self.shuffle_buffer = params.get("shuffle_buffer", 10 * self.batch_size)

        self.num_workers = params.get("num_workers", 0)
        self.drop_last = params.get("drop_last", True)
        self.prefetch_factor = params.get("prefetch_factor", 10)
        self.persistent_workers = params.get("persistent_workers", True)

        self.mixed_precision = params.get("mixed_precision")
        if self.mixed_precision:
            self.mp_type = (
                torch.bfloat16 if params["use_bfloat16"] else torch.float16
            )
        else:
            self.mp_type = torch.float32

        self.num_tasks = cm.num_streamers() if cm.is_streamer() else 1
        self.task_id = cm.get_streaming_rank() if cm.is_streamer() else 0

        # set later once processor gets a call to create a dataloader
        self.num_examples = 0
        self.files_in_this_task = []

        self.use_fast_dataloader = params.get("use_fast_dataloader", False)

        # Each activation worker can access entire dataset when True
        self.duplicate_act_worker_data = params.get(
            "duplicate_act_worker_data", False
        )
        self.disable_sharding = False

    @abstractmethod
    def _shard_files(self, is_training=False):
        pass

    @abstractmethod
    def _load_buffer(self, data_partitions):
        pass

    @abstractmethod
    def __getitem__(self):
        pass

    @abstractmethod
    def _shard_dataset(self, worker_id, num_workers):
        pass

    def __len__(self):
        """
        Returns the len of dataset on the task process
        """
        return self.num_examples

    def _worker_init_fn(self, worker_id):
        worker_info = torch.utils.data.get_worker_info()

        if worker_info is not None:
            worker_id = worker_info.id
            num_workers = worker_info.num_workers
        else:
            # Single-process
            worker_id = 0
            num_workers = 1

        self.data_partitions = self._maybe_shard_dataset(num_workers)

    def create_dataloader(self, is_training=True):
        """
        Classmethod to create the dataloader object.
        """
        self._shard_files(is_training)
        generator_fn = torch.Generator(device="cpu")

        if self.batch_size > self.num_examples // num_tasks():
            print(
                f"Dataset size: {len(self)} too small for num_tasks: {num_tasks()} and batch_size: {self.batch_size}, using duplicate data for activation workers..."
            )
            self.disable_sharding = True

        if self.shuffle:
            if self.duplicate_act_worker_data or self.disable_sharding:
                if self.shuffle_seed is None:
                    seed = task_id()
                else:
                    seed = self.shuffle_seed + task_id()
                random.seed(seed)
                random.shuffle(self.files_in_this_task)
                generator_fn.manual_seed(seed)
            data_sampler = torch.utils.data.RandomSampler(
                self, generator=generator_fn
            )
        else:
            data_sampler = torch.utils.data.SequentialSampler(self)

        if self.use_fast_dataloader:
            dataloader_fn = FastDataLoader
            print("-- Using FastDataLoader -- ")
        else:
            dataloader_fn = torch.utils.data.DataLoader

        data_loader = dataloader_fn(
            self,
            batch_size=self.batch_size,
            drop_last=self.drop_last,
            num_workers=self.num_workers,
            prefetch_factor=self.prefetch_factor if self.num_workers > 0 else 2,
            persistent_workers=self.persistent_workers
            if self.num_workers > 0
            else False,
            worker_init_fn=self._worker_init_fn,
            sampler=data_sampler,
        )
        # set self.data_partitions in case self.num_workers == 0
        if self.num_workers == 0:
            self._worker_init_fn(0)
        return data_loader

    def _apply_normalization(self, x):
        return normalize_tensor_transform(
            x, normalize_data_method=self.normalize_data_method
        )

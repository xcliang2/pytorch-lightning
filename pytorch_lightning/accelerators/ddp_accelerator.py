# Copyright The PyTorch Lightning team.
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
# limitations under the License
import os
import torch
import torch.distributed as torch_distrib
import subprocess
import sys
from os.path import abspath
from time import sleep
from typing import Optional
import numpy as np


from pytorch_lightning import _logger as log
from pytorch_lightning.utilities.distributed import find_free_network_port
from pytorch_lightning.accelerators.accelerator import Accelerator
from pytorch_lightning.utilities.distributed import rank_zero_only
from pytorch_lightning.utilities import AMPType
from pytorch_lightning.utilities.seed import seed_everything
from pytorch_lightning.distributed.dist import LightningDistributed
from pytorch_lightning.utilities.exceptions import MisconfigurationException
from pytorch_lightning.overrides.data_parallel import LightningDistributedDataParallel
from torch.nn.parallel import DistributedDataParallel
from typing import List


try:
    from hydra.utils import to_absolute_path, get_original_cwd
    from hydra.core.hydra_config import HydraConfig
except ImportError:
    HYDRA_AVAILABLE = False
else:
    HYDRA_AVAILABLE = True


class DDPAccelerator(Accelerator):

    def __init__(self, trainer, cluster_environment=None):
        super().__init__(trainer, cluster_environment)
        self.task_idx = None
        self._has_spawned_children = False
        self.interactive_ddp_procs = []
        self.dist = LightningDistributed()
        self.nickname = 'ddp'

    def setup(self, model):
        # first track model
        self.trainer.model = model

        # start the other scripts
        if os.environ.get('PL_IN_DDP_SUBPROCESS', '0') != '1':
            self._call_children_scripts()

        # set the task idx
        self.task_idx = int(os.environ['PL_DDP_PID'])

    def _call_children_scripts(self):
        assert self.trainer.global_rank == 0
        self._check_can_spawn_children()
        self._has_spawned_children = True

        os.environ['MASTER_ADDR'] = os.environ.get('MASTER_ADDR', '127.0.0.1')
        os.environ['MASTER_PORT'] = os.environ.get('MASTER_PORT', str(find_free_network_port()))

        # allow the user to pass the node rank
        node_rank = '0'
        node_rank = os.environ.get('NODE_RANK', node_rank)
        node_rank = os.environ.get('GROUP_RANK', node_rank)
        os.environ['NODE_RANK'] = node_rank
        os.environ['LOCAL_RANK'] = '0'

        # when user is using hydra find the absolute path
        path_lib = abspath if not HYDRA_AVAILABLE else to_absolute_path

        # pull out the commands used to run the script and resolve the abs file path
        command = sys.argv
        try:
            full_path = path_lib(command[0])
        except Exception as e:
            full_path = abspath(command[0])

        command[0] = full_path
        # use the same python interpreter and actually running
        command = [sys.executable] + command

        # the visible devices tell us how many GPUs we want to use.
        # when the trainer script was called the device has already been scoped by the time
        # code reaches this point. so, to call the scripts, we need to leave cuda visible devices alone
        # but forward the GPUs selected via environment variables
        if self.trainer.data_parallel_device_ids is None:
            raise MisconfigurationException('you selected (distribute_backend = ddp) but did not set Trainer(gpus=?)')

        os.environ['PL_TRAINER_GPUS'] = ','.join([str(i) for i in self.trainer.data_parallel_device_ids])
        os.environ['PL_IN_DDP_SUBPROCESS'] = '1'

        if self.trainer.logger is not None:
            os.environ['PL_EXP_VERSION'] = str(self.trainer.logger.version)

        gpu_ids = os.environ.get('CUDA_VISIBLE_DEVICES', '')
        if len(gpu_ids) == 1:
            gpu_ids = f'{gpu_ids},'

        num_gpus = max(1, len(gpu_ids.split(',')))

        os.environ['WORLD_SIZE'] = f'{num_gpus * self.trainer.num_nodes}'

        self.interactive_ddp_procs = []
        for local_rank in range(1, self.trainer.num_processes):
            env_copy = os.environ.copy()
            env_copy['LOCAL_RANK'] = f'{local_rank}'
            env_copy['PL_DDP_PID'] = str(self.trainer.data_parallel_device_ids[local_rank])
            # remove env var if global seed not set
            if os.environ.get('PL_GLOBAL_SEED') is None and 'PL_GLOBAL_SEED' in env_copy:
                del env_copy['PL_GLOBAL_SEED']

            # start process
            # if hydra is available and initialized, make sure to set the cwd correctly
            cwd: Optional[str] = None
            if HYDRA_AVAILABLE:
                if HydraConfig.initialized():
                    cwd = get_original_cwd()
            proc = subprocess.Popen(command, env=env_copy, cwd=cwd)
            self.interactive_ddp_procs.append(proc)

            # starting all processes at once can cause issues
            # with dataloaders delay between 1-10 seconds
            delay = np.random.uniform(1, 5, 1)[0]
            sleep(delay)

        os.environ['PL_DDP_PID'] = str(0)

    def train(self):
        model = self.trainer.model

        results = self.ddp_train(process_idx=self.task_idx, model=model)
        if 'WORLD_SIZE' in os.environ:
            del os.environ['WORLD_SIZE']
        return results

    def training_step(self, args):
        if self.trainer.amp_backend == AMPType.NATIVE:
            with torch.cuda.amp.autocast():
                output = self.trainer.model(*args)
        else:
            output = self.trainer.model(*args)
        return output

    def validation_step(self, args):
        output = self.training_step(args)
        return output

    def test_step(self, args):
        output = self.training_step(args)
        return output

    def barrier(self, name: Optional[str] = None):
        if torch_distrib.is_initialized():
            torch_distrib.barrier()

    def _check_can_spawn_children(self):
        if self._has_spawned_children:
            raise RuntimeError(
                "You tried to run `.fit` or `.test` multiple times in the same script."
                " This is not supported in DDP mode, switch to `distributed_backend='ddp_spawn'` instead."
            )

    def set_world_ranks(self, process_idx):
        self.trainer.local_rank = process_idx
        self.trainer.global_rank = self.trainer.node_rank * self.trainer.num_processes + process_idx
        self.trainer.world_size = self.trainer.num_nodes * self.trainer.num_processes

    def model_to_device(self, model, process_idx):
        self.trainer.root_gpu = process_idx
        torch.cuda.set_device(self.trainer.root_gpu)
        model.cuda(self.trainer.root_gpu)

    def get_device_ids(self):
        device_ids = [self.trainer.root_gpu]
        return device_ids

    def on_train_end(self):
        pass

    def early_stopping_should_stop(self, pl_module):
        stop = torch.tensor(int(self.trainer.should_stop), device=pl_module.device)
        torch_distrib.all_reduce(stop, op=torch_distrib.reduce_op.SUM)
        torch_distrib.barrier()
        should_stop = stop == self.trainer.world_size
        return should_stop

    def broadcast(self, obj, src=0):
        return self.dist.broadcast(obj)

    def ddp_train(self, process_idx, model):
        """
        Entry point for ddp

        Args:
            process_idx:
            mp_queue: multiprocessing queue
            model:

        Returns:

        """
        seed = os.environ.get("PL_GLOBAL_SEED")
        if seed is not None:
            seed_everything(int(seed))

        # show progressbar only on progress_rank 0
        if (self.trainer.node_rank != 0 or process_idx != 0) and self.trainer.progress_bar_callback is not None:
            self.trainer.progress_bar_callback.disable()

        # determine which process we are and world size
        self.set_world_ranks(process_idx)

        # set warning rank
        rank_zero_only.rank = self.trainer.global_rank

        # set up server using proc 0's ip address
        # try to init for 20 times at max in case ports are taken
        # where to store ip_table
        model.trainer = self.trainer
        self.init_ddp_connection(
            self.trainer.global_rank,
            self.trainer.world_size,
            self.trainer.is_slurm_managing_tasks
        )

        # call setup after the ddp process has connected
        self.trainer.call_setup_hook(model)

        # on world_size=0 let everyone know training is starting
        if self.trainer.is_global_zero and not torch.distributed.is_initialized():
            log.info('-' * 100)
            log.info(f'distributed_backend={self.trainer.distributed_backend}')
            log.info(f'All DDP processes registered. Starting ddp with {self.trainer.world_size} processes')
            log.info('-' * 100)

        # call sync_bn before .cuda(), configure_apex and configure_ddp
        if self.trainer.sync_batchnorm:
            model = self.configure_sync_batchnorm(model)

        # move the model to the correct device
        self.model_to_device(model, process_idx)

        # CHOOSE OPTIMIZER
        # allow for lr schedulers as well
        self.setup_optimizers(model)

        # set model properties before going into wrapper
        self.trainer.model_connector.copy_trainer_model_properties(model)

        # 16-bit
        model = self.trainer.precision_connector.connect(model)

        # device ids change depending on the DDP setup
        device_ids = self.get_device_ids()

        # allow user to configure ddp
        model = self.configure_ddp(model, device_ids)

        # set up training routine
        self.barrier('ddp_setup')
        self.trainer.train_loop.setup_training(model)

        # train or test
        results = self.train_or_test()

        # clean up memory
        torch.cuda.empty_cache()

        return results

    def configure_ddp(
        self, model: "LightningModule", device_ids: List[int]
    ) -> DistributedDataParallel:
        model = LightningDistributedDataParallel(
            model, device_ids=device_ids, find_unused_parameters=True
        )
        return model

    def configure_sync_batchnorm(self, model: "LightningModule") -> "LightningModule":
        """
        Add global batchnorm for a model spread across multiple GPUs and nodes.

        Override to synchronize batchnorm between specific process groups instead
        of the whole world or use a different sync_bn like `apex`'s version.

        Args:
            model: pointer to current :class:`LightningModule`.

        Return:
            LightningModule with batchnorm layers synchronized between process groups
        """
        model = torch.nn.SyncBatchNorm.convert_sync_batchnorm(model, process_group=None)

        return model

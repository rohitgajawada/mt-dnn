# coding=utf-8
# Copyright (c) Microsoft. All rights reserved.
import logging
import sys

import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F
import torch.optim as optim
from torch.optim.lr_scheduler import *
from data_utils.utils import AverageMeter
from pytorch_pretrained_bert import BertAdam as Adam
from module.bert_optim import Adamax, RAdam
#from module.my_optim import EMA
from mt_dnn.loss import LOSS_REGISTRY
from .matcher import SANBertNetwork

from data_utils.task_def import TaskType

logger = logging.getLogger(__name__)


class MTDNNModel(object):
    def __init__(self, opt, state_dict=None, num_train_step=-1):
        self.config = opt
        self.updates = state_dict['updates'] if state_dict and 'updates' in state_dict else 0
        self.local_updates = 0
        self.train_loss = AverageMeter()
        self.network = SANBertNetwork(opt)
        if state_dict:
            self.network.load_state_dict(state_dict['state'], strict=False)
        self.mnetwork = nn.DataParallel(self.network) if opt['multi_gpu_on'] else self.network
        self.total_param = sum([p.nelement() for p in self.network.parameters() if p.requires_grad])
        if opt['cuda']:
            self.network.cuda()
        optimizer_parameters = self._get_param_groups()
        self._setup_optim(optimizer_parameters, state_dict, num_train_step)
        self.para_swapped = False
        self.optimizer.zero_grad()
        self._setup_lossmap(self.config)

    def _get_param_groups(self):
        no_decay = ['bias', 'gamma', 'beta', 'LayerNorm.bias', 'LayerNorm.weight']
        optimizer_parameters = [
            {'params': [p for n, p in self.network.named_parameters() if not any(nd in n for nd in no_decay)],
             'weight_decay': 0.01},
            {'params': [p for n, p in self.network.named_parameters() if any(nd in n for nd in no_decay)],
             'weight_decay': 0.0}
        ]
        return optimizer_parameters

    def _setup_optim(self, optimizer_parameters, state_dict=None, num_train_step=-1):
        if self.config['optimizer'] == 'sgd':
            self.optimizer = optim.SGD(optimizer_parameters, self.config['learning_rate'],
                                       weight_decay=self.config['weight_decay'])

        elif self.config['optimizer'] == 'adamax':
            self.optimizer = Adamax(optimizer_parameters,
                                    self.config['learning_rate'],
                                    warmup=self.config['warmup'],
                                    t_total=num_train_step,
                                    max_grad_norm=self.config['grad_clipping'],
                                    schedule=self.config['warmup_schedule'],
                                    weight_decay=self.config['weight_decay'])
            if self.config.get('have_lr_scheduler', False): self.config['have_lr_scheduler'] = False
        elif self.config['optimizer'] == 'radam':
            self.optimizer = RAdam(optimizer_parameters,
                                    self.config['learning_rate'],
                                    warmup=self.config['warmup'],
                                    t_total=num_train_step,
                                    max_grad_norm=self.config['grad_clipping'],
                                    schedule=self.config['warmup_schedule'],
                                    eps=self.config['adam_eps'],
                                    weight_decay=self.config['weight_decay'])
            if self.config.get('have_lr_scheduler', False): self.config['have_lr_scheduler'] = False
            # The current radam does not support FP16.
            self.config['fp16'] = False
        elif self.config['optimizer'] == 'adam':
            self.optimizer = Adam(optimizer_parameters,
                                  lr=self.config['learning_rate'],
                                  warmup=self.config['warmup'],
                                  t_total=num_train_step,
                                  max_grad_norm=self.config['grad_clipping'],
                                  schedule=self.config['warmup_schedule'],
                                  weight_decay=self.config['weight_decay'])
            if self.config.get('have_lr_scheduler', False): self.config['have_lr_scheduler'] = False
        else:
            raise RuntimeError('Unsupported optimizer: %s' % opt['optimizer'])

        if state_dict and 'optimizer' in state_dict:
            self.optimizer.load_state_dict(state_dict['optimizer'])

        if self.config['fp16']:
            try:
                from apex import amp
                global amp
            except ImportError:
                raise ImportError("Please install apex from https://www.github.com/nvidia/apex to use fp16 training.")
            model, optimizer = amp.initialize(self.network, self.optimizer, opt_level=self.config['fp16_opt_level'])
            self.network = model
            self.optimizer = optimizer

        if self.config.get('have_lr_scheduler', False):
            if self.config.get('scheduler_type', 'rop') == 'rop':
                self.scheduler = ReduceLROnPlateau(self.optimizer, mode='max', factor=self.config['lr_gamma'], patience=3)
            elif self.config.get('scheduler_type', 'rop') == 'exp':
                self.scheduler = ExponentialLR(self.optimizer, gamma=self.config.get('lr_gamma', 0.95))
            else:
                milestones = [int(step) for step in self.config.get('multi_step_lr', '10,20,30').split(',')]
                self.scheduler = MultiStepLR(self.optimizer, milestones=milestones, gamma=self.config.get('lr_gamma'))
        else:
            self.scheduler = None

    def _setup_lossmap(self, config):
        loss_types = config['loss_types']
        self.task_loss_criterion = []
        for idx, cs in enumerate(loss_types):
            print(cs)
            assert cs is not None
            lc = LOSS_REGISTRY[cs](name='Loss func of task {}: {}'.format(idx, cs))
            self.task_loss_criterion.append(lc)

    def train(self):
        if self.para_swapped:
            #self.ema.swap_parameters()
            self.para_swapped = False

    def update(self, batch_meta, batch_data):
        self.network.train()
        y = batch_data[batch_meta['label']]
        soft_labels = None
        #if self.config.get('mkd_opt', 0) > 0 and ('soft_label' in batch_meta):
        #    soft_labels = batch_meta['soft_label']

        task_type = batch_meta['task_type']
        if self.config['cuda']:
            if isinstance(y, list) or isinstance(y, tuple):
                y = [e.cuda(non_blocking=True) for e in y]
                for e in y:
                    e.requires_grad = False
            else:
                y = y.cuda(non_blocking=True)
                y.requires_grad = False

        task_id = batch_meta['task_id']
        inputs = batch_data[:batch_meta['input_len']]
        if len(inputs) == 3:
            inputs.append(None)
            inputs.append(None)
        inputs.append(task_id)
        weight = None
        if self.config.get('weighted_on', False):
            if self.config['cuda']:
                weight = batch_data[batch_meta['factor']].cuda(non_blocking=True)
            else:
                weight = batch_data[batch_meta['factor']]
        logits = self.mnetwork(*inputs)

        # comput loss
        loss = self.task_loss_criterion[task_id](logits, y, weight, ignore_index=-1)

        self.train_loss.update(loss.item(), batch_data[batch_meta['token_id']].size(0))
        # scale loss
        loss = loss / self.config.get('grad_accumulation_step', 1)
        if self.config['fp16']:
            with amp.scale_loss(loss, self.optimizer) as scaled_loss:
                scaled_loss.backward()
        else:
            loss.backward()
        self.local_updates += 1
        if self.local_updates % self.config.get('grad_accumulation_step', 1) == 0:
            if self.config['global_grad_clipping'] > 0:
                if self.config['fp16']:
                    torch.nn.utils.clip_grad_norm_(amp.master_params(self.optimizer),
                                                   self.config['global_grad_clipping'])
                else:
                    torch.nn.utils.clip_grad_norm_(self.network.parameters(),
                                                  self.config['global_grad_clipping'])
            self.updates += 1
            # reset number of the grad accumulation
            self.optimizer.step()
            self.optimizer.zero_grad()

    def predict(self, batch_meta, batch_data):
        self.network.eval()
        task_id = batch_meta['task_id']
        task_type = batch_meta['task_type']
        inputs = batch_data[:batch_meta['input_len']]
        if len(inputs) == 3:
            inputs.append(None)
            inputs.append(None)
        inputs.append(task_id)
        score = self.mnetwork(*inputs)
        if task_type == TaskType.Ranking:
            score = score.contiguous().view(-1, batch_meta['pairwise_size'])
            assert task_type == TaskType.Ranking
            score = F.softmax(score, dim=1)
            score = score.data.cpu()
            score = score.numpy()
            predict = np.zeros(score.shape, dtype=int)
            positive = np.argmax(score, axis=1)
            for idx, pos in enumerate(positive):
                predict[idx, pos] = 1
            predict = predict.reshape(-1).tolist()
            score = score.reshape(-1).tolist()
            return score, predict, batch_meta['true_label']
        elif task_type == TaskType.SeqenceLabeling:
            mask = batch_data[batch_meta['mask']]
            score = score.contiguous()
            score = score.data.cpu()
            score = score.numpy()
            predict = np.argmax(score, axis=1).reshape(mask.size()).tolist()
            valied_lenght = mask.sum(1).tolist()
            final_predict = []
            for idx, p in enumerate(predict):
                final_predict.append(p[: valied_lenght[idx]])
            score = score.reshape(-1).tolist()
            return score, final_predict, batch_meta['label']
        else:
            if task_type == TaskType.Classification:
                score = F.softmax(score, dim=1)
            score = score.data.cpu()
            score = score.numpy()
            predict = np.argmax(score, axis=1).tolist()
            score = score.reshape(-1).tolist()
        return score, predict, batch_meta['label']

    def extract(self, batch_meta, batch_data):
        self.network.eval()
        # 'token_id': 0; 'segment_id': 1; 'mask': 2
        inputs = batch_data[:3]
        all_encoder_layers, pooled_output = self.mnetwork.bert(*inputs)
        return all_encoder_layers, pooled_output

    def save(self, filename):
        network_state = dict([(k, v.cpu()) for k, v in self.network.state_dict().items()])
        #ema_state = dict(
        #    [(k, v.cpu()) for k, v in self.ema.model.state_dict().items()]) if self.ema is not None else dict()
        params = {
            'state': network_state,
            'optimizer': self.optimizer.state_dict(),
            #'ema': ema_state,
            'config': self.config,
        }
        torch.save(params, filename)
        logger.info('model saved to {}'.format(filename))

    def load(self, checkpoint):
        model_state_dict = torch.load(checkpoint)
        if model_state_dict['config']['init_checkpoint'].rsplit('/', 1)[1] != \
                self.config['init_checkpoint'].rsplit('/', 1)[1]:
            logger.error(
                '*** SANBert network is pretrained on a different Bert Model. Please use that to fine-tune for other tasks. ***')
            sys.exit()

        self.network.load_state_dict(model_state_dict['state'], strict=False)
        self.optimizer.load_state_dict(model_state_dict['optimizer'])
        self.config = model_state_dict['config']

    def cuda(self):
        self.network.cuda()
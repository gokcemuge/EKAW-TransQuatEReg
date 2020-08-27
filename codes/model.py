#!/usr/bin/python3

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import logging
import datetime

import numpy as np

import torch
import torch.nn as nn
import torch.nn.functional as F

import numpy as np
from numpy.random import RandomState
import os
import io

from sklearn.metrics import average_precision_score

from torch.utils.data import DataLoader

from dataloader import TestDataset
import io
import pandas as pd

# regularization terms options
L2 = True
L1 = False
L2_COEFF = 0.00002

PROJECT_CUBE = False
PROJECT_SPHERE = False


class KGEModel(nn.Module):
    def __init__(self, model_name, nentity, nrelation, ntriples, hidden_dim, args):
        super(KGEModel, self).__init__()
        '''
        if L2:
            print('L2 regul with coeff ', L2_COEFF)
        if L1:
            print('L1 regul with coeff ', L2_COEFF)
        '''
        print("torch version:", torch.__version__)

        self.training_start_time = datetime.datetime.now().strftime('%Y%m%d_%H%M')
        self.model_name = model_name
        self.nentity = nentity
        self.nrelation = nrelation
        self.epsilon = 2.0
        self.lmbda = 0.1  # lambda for QuatE loss
        self.hidden_dim = hidden_dim
        self.idx = 1
        self.ruge_rule_penalty = 1
        self.alpha = 1000

        self.gamma = nn.Parameter(
            torch.Tensor([args.gamma]),
            requires_grad=False
        )

        self.embedding_range = nn.Parameter(
            torch.Tensor([(self.gamma.item() + self.epsilon) / hidden_dim]),
            requires_grad=False
        )

        # self.xi = nn.Parameter(torch.zeros(ntriples, 1))
        # nn.init.uniform_(
        #    tensor=self.xi,
        #    a=-0.1,
        #    b=0.1
        # )

        # self.xi_neg = nn.Parameter(torch.zeros(ntriples, 1))
        # nn.init.uniform_(
        #    tensor=self.xi_neg,
        #    a=-0.1,
        #    b=0.1
        # )

        ent_dim_mult, rel_dim_mult = self.compute_multipliers()

        # define entity and relation embeddings
        self.entity_dim = ent_dim_mult * hidden_dim
        self.relation_dim = rel_dim_mult * hidden_dim

        self.entity_embedding = nn.Parameter(torch.zeros(nentity, self.entity_dim))
        self.relation_embedding = nn.Parameter(torch.zeros(self.nrelation, self.relation_dim))
        self.initialize(self.entity_embedding, nentity, hidden_dim)
        self.initialize(self.relation_embedding, nrelation, hidden_dim)
        # setup multipliers
        if self.model_name in ['TransRotatE', 'TransQuatE', 'QuatEVersor']:
            mult = 4 if 'Quat' in self.model_name else 1
            self.rotator_head = nn.Parameter(torch.zeros(nrelation, mult * hidden_dim))
            self.rotator_tail = nn.Parameter(torch.zeros(nrelation, mult * hidden_dim))
            self.initialize(self.rotator_head, nrelation, hidden_dim)
            self.initialize(self.rotator_tail, nrelation, hidden_dim)

        if self.model_name == 'sTransRotatE' or self.model_name == 'sTransQuatE':
            self.rotator_head = nn.Parameter(torch.zeros(nrelation, hidden_dim))
            self.initialize(self.rotator_head, nrelation, hidden_dim)

        if self.model_name in ['TransQuatEReg']:
            mult = 4 if 'Quat' in self.model_name else 1
            self.rotator_head = nn.Parameter(torch.zeros(nrelation, mult * hidden_dim))
            self.mapping_regularizer = nn.Parameter(torch.zeros(nentity, mult * hidden_dim))
            self.initialize(self.rotator_head, nrelation, hidden_dim)
            self.initialize(self.mapping_regularizer, nentity, hidden_dim)

        # set rule info
        self.ruge = args.ruge

        self.epsilon_inv = .1
        self.epsilon_impl = .1
        self.epsilon_eq = 0
        self.epsilon_sym = 0
        self.inject = args.inject

        self.rule_weight = {
            'inverse': 2.0,
            'implication': 1.0,
            'symmetry': .01,
            'equality': .01,
            'ruge': 1
        }

        if model_name == 'pRotatE':
            self.modulus = nn.Parameter(torch.Tensor([[0.5 * self.embedding_range.item()]]))

        self.gamma1 = 23;
        self.gamma2 = 25

        # Do not forget to modify this line when you add a new model in the "forward" function
        if model_name not in ['TransE', 'DistMult', 'ComplEx', 'RotatE', 'pRotatE', 'TransRotatE', 'QuatE',
                              'TransQuatE', 'sTransRotatE', 'sTransQuatE', 'QuatEVersor', 'TransQuatEReg']:
            raise ValueError('model %s not supported' % model_name)

    def load_entities(self, args):
        data_path = args.data_path
        all_entities = pd.read_csv(os.path.join(data_path, 'entities.dict'), delimiter='\t', names=["id", "entities"],
                                   dtype=str)
        all_entities = pd.DataFrame(all_entities)

        return all_entities

    def load_relations(self, args):
        data_path = args \
            .data_path
        all_relations = pd.read_csv(os.path.join(data_path, 'relations.dict'), delimiter='\t',
                                    names=["id", "relations"],
                                    dtype=str)
        all_relations = pd.DataFrame(all_relations)
        return all_relations

    def compute_multipliers(self):
        if self.model_name == 'RotatE':
            return 2, 1
        if self.model_name in ['TransRotatE', 'ComplEx', 'DistMult', 'sTransRotatE']:
            return 2, 2
        if self.model_name in ['QuatE', 'TransQuatE', 'sTransQuatE', 'QuatEVersor', 'TransQuatEReg']:
            return 4, 4
        return 1, 1

    def initialize(self, tensor, in_features, out_features):
        if 'Quat' not in self.model_name:
            nn.init.uniform_(
                tensor=tensor,
                a=-self.embedding_range.item(),
                b=self.embedding_range.item()
            )

        else:  # use quaternion initialization
            fan_in = in_features
            fan_out = out_features

            s = 1. / np.sqrt(2 * in_features)

            rng = torch.random.manual_seed(42)

            # Generating randoms and purely imaginary quaternions :
            kernel_shape = (in_features, out_features)
            nweigths = in_features * out_features

            wi = torch.FloatTensor(nweigths).uniform_()
            wj = torch.FloatTensor(nweigths).uniform_()
            wk = torch.FloatTensor(nweigths).uniform_()

            # Purely imaginary quaternions unitary
            norms = torch.sqrt(wi ** 2 + wj ** 2 + wk ** 2) + 0.0001
            wi /= norms
            wj /= norms
            wk /= norms

            wi = wi.reshape(kernel_shape)
            wj = wj.reshape(kernel_shape)
            wk = wk.reshape(kernel_shape)

            modulus = torch.zeros(kernel_shape).uniform_(-s, s)
            phase = torch.zeros(kernel_shape).uniform_(-np.pi, np.pi)

            weight_r = modulus * torch.cos(phase)
            weight_i = modulus * wi * torch.sin(phase)
            weight_j = modulus * wj * torch.sin(phase)
            weight_k = modulus * wk * torch.sin(phase)

            tensor.data = torch.cat((weight_r, weight_i, weight_j, weight_k), dim=1)

    def set_loss(self, loss_name):
        self.loss_name = loss_name
        loss_fnc = {
            'rotate': self.rotate_loss,
            'custom': self.custom_loss,
            'adaptive_margin': self.adaptive_margin_loss,
            'quate': self.quate_loss,
            'ruge': self.ruge_loss,
            'bce': self.bce_logits_loss,  # ruge loss; to test models with and without ruge rule addition
            'uncertain_loss': self.uncertain_loss,
            'limit_loss': self.Limit_Loss
        }

        if loss_name == 'quate':
            self.criterion = nn.Softplus()

        if loss_name == 'limit_loss':
            self.lda = 0.01
            print(self.lda)

        if loss_name == 'adaptive_margin':
            self.margin = nn.Parameter(torch.FloatTensor([0.1]), requires_grad=True)

            self.lambda1 = 1.
            self.sigma = 1.01000100

        if loss_name == 'ruge':
            self.criterion = nn.BCEWithLogitsLoss(reduction='sum')
            self.ruge_rule_penalty = .01

        if loss_name not in loss_fnc:
            raise ValueError('model %s not supported' % loss_name)

        self.Loss = loss_fnc[loss_name]

    def select_relations(self, indices):
        relation_dict = {}
        if self.model_name in ['biRotatE', 'TransRotatE', 'TransQuatE', 'QuatEVersor']:
            relation_head = torch.index_select(
                self.rotator_head,
                dim=0,
                index=indices
            ).unsqueeze(1)

            relation_tail = torch.index_select(
                self.rotator_tail,
                dim=0,
                index=indices
            ).unsqueeze(1)
            relation_dict = {'rotator_head': relation_head, 'rotator_tail': relation_tail}
            # relation_list = [relation_head, relation_tail]

        if self.model_name in ['TransQuatEReg']:
            rotator_head = torch.index_select(
                self.rotator_head,
                dim=0,
                index=indices
            ).unsqueeze(1)
            # relation_dict = {'rotator_head': rotator_head}
            relation_dict['rotator_head'] = rotator_head
            # relation_list = [relation_head, relation_tail]

        if self.model_name == 'sTransRotatE' or self.model_name == 'sTransQuatE':
            relation_head = torch.index_select(
                self.rotator_head,
                dim=0,
                index=indices
            ).unsqueeze(1)
            # relation_list = [relation_head]
            relation_dict = {'rotator_head': relation_head}

        if self.model_name != 'biRotatE':
            relation = torch.index_select(
                self.relation_embedding,
                dim=0,
                index=indices
            ).unsqueeze(1)
            relation_dict['translation'] = relation
            # relation_list.append(relation)

        return relation_dict

    def entities_select(self, indices_heads, indices_tails):
        head = torch.index_select(
            self.entity_embedding,
            dim=0,
            index=indices_heads)

        tail = torch.index_select(
            self.entity_embedding,
            dim=0,
            index=indices_tails)

        if self.model_name in ['TransQuatEReg']:
            mapping_regularizer = torch.index_select(
                self.mapping_regularizer,
                dim=0,
                index=indices_heads)  # TODO: checked

            # entity_dict = {'mapping_reqularizer': mapping_reqularizer, 'head': head, 'tail': tail}
            return head, tail, mapping_regularizer

        return head, tail

    def forward(self, idx, sample, mode='single'):
        '''
        Forward function that calculate the score of a batch of triples.
        In the 'single' mode, sample is a batch of triple.
        In the 'head-batch' or 'tail-batch' mode, sample consists two part.
        The first part is usually the positive sample.
        And the second part is the entities in the negative samples.
        Because negative samples and positive samples usually share two elements
        in their triple ((head, relation) or (relation, tail)).
        '''
        self.idx = idx

        relation_list = []

        if self.model_name in ['TransQuatEReg']:
            if mode == 'single':
                batch_size, negative_sample_size = sample.size(0), 1
                head, tail, mapping_regularizer = self.entities_select(sample[:, 0], sample[:, 2])
                head = head.unsqueeze(1)
                tail = tail.unsqueeze(1)
                mapping_regularizer = mapping_regularizer.unsqueeze(1)

                relation_dict = self.select_relations(sample[:, 1])

            elif mode == 'head-batch':
                tail_part, head_part = sample
                # tail part has positive triples  h,r,t
                # head_part has corrupted heads only h'X 10
                batch_size, negative_sample_size = head_part.size(0), head_part.size(1)

                head, tail, mapping_regularizer = self.entities_select(head_part.view(-1), tail_part[:, 2])
                head = head.view(batch_size, negative_sample_size, -1)
                tail = tail.unsqueeze(1)
                mapping_regularizer = mapping_regularizer.view(batch_size, negative_sample_size, -1)

                relation_dict = self.select_relations(tail_part[:, 1])

            elif mode == 'tail-batch':
                head_part, tail_part = sample
                # head_part has positive triples  h,r,t
                # tail_part has corrupted tails only h'X 10

                batch_size, negative_sample_size = tail_part.size(0), tail_part.size(1)
                head, tail, mapping_regularizer = self.entities_select(head_part[:, 0], tail_part.view(-1))
                head = head.unsqueeze(1)
                tail = tail.view(batch_size, negative_sample_size, -1)
                mapping_regularizer = mapping_regularizer.unsqueeze(1)

                relation_dict = self.select_relations(head_part[:, 1])

            else:
                raise ValueError('mode %s not supported' % mode)
            arg_dict = {
                'head': head,
                **relation_dict,
                'tail': tail,
                'mapping_regularizer': mapping_regularizer
            }
        else:
            if mode == 'single':
                batch_size, negative_sample_size = sample.size(0), 1
                head, tail = self.entities_select(sample[:, 0], sample[:, 2])
                head = head.unsqueeze(1)
                tail = tail.unsqueeze(1)

                relation_dict = self.select_relations(sample[:, 1])

            elif mode == 'head-batch':
                tail_part, head_part = sample
                batch_size, negative_sample_size = head_part.size(0), head_part.size(1)

                head, tail = self.entities_select(head_part.view(-1), tail_part[:, 2])
                head = head.view(batch_size, negative_sample_size, -1)
                tail = tail.unsqueeze(1)

                relation_dict = self.select_relations(tail_part[:, 1])

            elif mode == 'tail-batch':
                head_part, tail_part = sample
                batch_size, negative_sample_size = tail_part.size(0), tail_part.size(1)
                head, tail = self.entities_select(head_part[:, 0], tail_part.view(-1))
                head = head.unsqueeze(1)
                tail = tail.view(batch_size, negative_sample_size, -1)

                relation_dict = self.select_relations(head_part[:, 1])

            else:
                raise ValueError('mode %s not supported' % mode)
            arg_dict = {
                'head': head,
                **relation_dict,
                'tail': tail

            }
        # arg_list = [head] + relation_list + [tail]
        # arg_list += [mode] if self.model_name not in ['biRotatE', 'TransRotatE', 'TransQuatE', 'QuatE', 'sTransQuatE','sTransRotatE'] else []
        if self.model_name not in ['biRotatE', 'TransRotatE', 'TransQuatE', 'QuatE', 'sTransQuatE', 'sTransRotatE',
                                   'QuatEVersor', 'TransQuatEReg']:
            arg_dict['mode'] = mode
        score = self.compute_score(arg_dict)
        return score

    def compute_score(self, arg_dict):
        model_func = {
            'TransE': self.TransE,
            'DistMult': self.DistMult,
            'ComplEx': self.ComplEx,
            'RotatE': self.RotatE,
            'pRotatE': self.pRotatE,
            'biRotatE': self.biRotatE,
            'TransRotatE': self.TransRotatE,
            'QuatE': self.QuatE,
            'TransQuatE': self.TransQuatE,
            'sTransRotatE': self.HalfRotatE,
            'sTransQuatE': self.halfTransQuatE,
            'QuatEVersor': self.QuatEVersor,
            'TransQuatEReg': self.TransQuatEReg
        }

        score = model_func[self.model_name](**arg_dict)
        return score

    def TransE(self, head, translation, tail, mode):
        if mode == 'head-batch':
            score = head + (translation - tail)
        else:
            score = (head + translation) - tail
        score = torch.norm(score, p=1, dim=2)
        return score
        # return self.gamma.item() - score

    def DistMult(self, head, translation, tail, mode):
        if mode == 'head-batch':
            score = head * (translation * tail)
        else:
            score = (head * translation) * tail

        score = score.sum(dim=2)
        return score

    def ComplEx(self, head, translation, tail, mode):
        re_head, im_head = torch.chunk(head, 2, dim=2)
        re_relation, im_relation = torch.chunk(translation, 2, dim=2)
        re_tail, im_tail = torch.chunk(tail, 2, dim=2)
        '''if mode == 'head-batch':  # take complex conjugate of head
                                    score = re_head*re_relation*re_tail + \
                                            re_relation*im_head*im_tail - \
                                            im_relation*re_head*im_tail + \
                                            im_relation*im_head*re_tail
                                else:
                                    score = re_relation*re_head*re_tail + \
                                            re_relation*im_head*im_tail + \
                                            im_relation*re_head*im_tail - \
                                            im_relation*im_head*re_tail
                                score = score.sum(dim = 2)
                                return score'''

        if mode == 'head-batch':
            re_score = re_relation * re_tail + im_relation * im_tail
            im_score = re_relation * im_tail - im_relation * re_tail
            score = re_head * re_score + im_head * im_score
        else:
            re_score = re_head * re_relation - im_head * im_relation
            im_score = re_head * im_relation + im_head * re_relation
            score = re_score * re_tail + im_score * im_tail

        score = score.sum(dim=2)
        return score

    def RotatE(self, head, translation, tail, mode):
        pi = 3.14159265358979323846

        re_head, im_head = torch.chunk(head, 2, dim=2)
        re_tail, im_tail = torch.chunk(tail, 2, dim=2)

        # Make phases of relations uniformly distributed in [-pi, pi]

        '''phase_relation = relation/(self.embedding_range.item()/pi)

            re_relation = torch.cos(phase_relation)
            im_relation = torch.sin(phase_relation)
        '''
        re_relation, im_relation = self.extract_relations(translation)
        if mode == 'head-batch':
            re_score = re_relation * re_tail + im_relation * im_tail
            im_score = re_relation * im_tail - im_relation * re_tail
            re_score = re_score - re_head
            im_score = im_score - im_head
        else:
            re_score = re_head * re_relation - im_head * im_relation
            im_score = re_head * im_relation + im_head * re_relation
            re_score = re_score - re_tail
            im_score = im_score - im_tail

        score = torch.stack([re_score, im_score], dim=0)
        score = score.norm(dim=0)
        score = score.sum(dim=2)
        return score

    def extract_relations(self, *args):
        pi = 3.14159265358979323846

        split_relations = []
        for relation in args:
            phase_relation = relation / (self.embedding_range.item() / pi)
            re_relation = torch.cos(phase_relation)
            im_relation = torch.sin(phase_relation)
            split_relations.extend([re_relation, im_relation])
        return split_relations

    def TransRotatE(self, head, rotator_head, rotator_tail, translation, tail):
        re_head, im_head = torch.chunk(head, 2, dim=2)
        re_tail, im_tail = torch.chunk(tail, 2, dim=2)

        # multipliers
        re_relation_head, im_relation_head, re_relation_tail, im_relation_tail = self.extract_relations(
            rotator_head, rotator_tail)

        re_translation, im_translation = torch.chunk(translation, 2, dim=2)

        re_score_head = re_relation_head * re_head - im_relation_head * im_head
        im_score_head = re_relation_head * im_head + im_relation_head * re_head

        re_score_tail = re_relation_tail * re_tail + im_relation_tail * im_tail
        im_score_tail = re_relation_tail * im_tail - im_relation_tail * re_tail

        re_score = re_score_head + re_translation - re_score_tail
        im_score = im_score_head + re_translation - im_score_tail
        score = torch.stack([re_score, im_score], dim=0)
        score = torch.norm(score, p=1, dim=0)
        score = score.sum(dim=2)
        return score

    def HalfRotatE(self, head, rotator_head, translation, tail):
        re_head, im_head = torch.chunk(head, 2, dim=2)
        re_tail, im_tail = torch.chunk(tail, 2, dim=2)

        # multipliers
        re_relation_head, im_relation_head = self.extract_relations(rotator_head)

        re_translation, im_translation = torch.chunk(translation, 2, dim=2)

        re_score_head = re_relation_head * re_head - im_relation_head * im_head
        im_score_head = re_relation_head * im_head + im_relation_head * re_head

        # re_score_tail = re_relation_tail * re_tail + im_relation_tail * im_tail
        # im_score_tail = re_relation_tail * im_tail - im_relation_tail * re_tail

        re_score = re_score_head + re_translation - re_tail
        im_score = im_score_head + re_translation + im_tail
        score = torch.stack([re_score, im_score], dim=0)
        score = torch.norm(score, p=1, dim=0)
        score = score.sum(dim=2)
        return score

    def biRotatE(self, head, rotator_head, rotator_tail, tail):
        '''
        Using double rotate computation the score
        '''

        re_head, im_head = torch.chunk(head, 2, dim=2)
        re_tail, im_tail = torch.chunk(tail, 2, dim=2)

        re_relation_head, im_relation_head, re_relation_tail, im_relation_tail \
            = self.extract_relations(rotator_head, rotator_tail)
        # Make phases of relations uniformly distributed in [-pi, pi]

        re_score = re_relation_head * re_head - im_relation_head * im_head
        im_score = re_relation_head * im_head + im_relation_head * re_head

        re_score_tail = re_relation_tail * re_tail + im_relation_tail * im_tail
        im_score_tail = re_relation_tail * im_tail - im_relation_tail * re_tail

        re_score = re_score - re_score_tail
        im_score = im_score - im_score_tail
        score = torch.stack([re_score, im_score], dim=0)
        score = score.norm(dim=0)
        score = score.sum(dim=2)
        return score

    def pRotatE(self, head, translation, tail, mode):
        pi = 3.14159262358979323846

        # Make phases of entities and relations uniformly distributed in [-pi, pi]
        phase_head = head / (self.embedding_range.item() / pi)
        phase_relation = translation / (self.embedding_range.item() / pi)
        phase_tail = tail / (self.embedding_range.item() / pi)

        if mode == 'head-batch':
            score = phase_head + (phase_relation - phase_tail)
        else:
            score = (phase_head + phase_relation) - phase_tail

        score = torch.sin(score)
        score = torch.abs(score)

        score = self.gamma.item() - score.sum(dim=2) * self.modulus
        return score

    def normalize_quaternion(self, tensor):
        t, ti, tj, tk = torch.chunk(tensor, 4, dim=2)
        denom = torch.sqrt(t ** 2 + ti ** 2 + tj ** 2 + tk ** 2)
        t = t / denom
        ti = ti / denom
        tj = tj / denom
        tk = tk / denom
        return t, ti, tj, tk

    def halfTransQuatE(self, head, rotator_head, translation, tail):
        h, hi, hj, hk = torch.chunk(head, 4, dim=2)
        t, ti, tj, tk = torch.chunk(tail, 4, dim=2)

        ti, tj, tk = -ti, -tj, -tk
        tran, tran_i, tran_j, tran_k = torch.chunk(translation, 4, dim=2)  # translations
        rot_h, rot_hi, rot_hj, rot_hk = self.normalize_quaternion(rotator_head)

        # Rotate head
        rotated_head_real = h * rot_h - hi * rot_hi - hj * rot_hj - hk * rot_hk
        rotated_head_i = h * rot_hi + hi * rot_h + hj * rot_hk - hk * rot_hj
        rotated_head_j = h * rot_hj - hi * rot_hk + hj * rot_h + hk * rot_hi
        rotated_head_k = h * rot_hk + hi * rot_hj - hj * rot_hi + hk * rot_h

        # translate and score
        score_r = rotated_head_real + tran - t
        score_i = rotated_head_i + tran_i - ti
        score_j = rotated_head_j + tran_j - tk
        score_k = rotated_head_k + tran_k - tj

        score = torch.stack([score_r, score_i, score_j, score_k], dim=0)
        score = torch.norm(score, dim=0).sum(dim=2)

        return score

    def QuatE(self, head, translation, tail):
        h, hi, hj, hk = torch.chunk(head, 4, dim=2)
        t, ti, tj, tk = torch.chunk(tail, 4, dim=2)

        r, ri, rj, rk = self.normalize_quaternion(translation)

        rotated_head_real = h * r - hi * ri - hj * rj - hk * rk
        rotated_head_i = h * ri + hi * r + hj * rk - hk * rj
        rotated_head_j = h * rj - hi * rk + hj * r + hk * ri
        rotated_head_k = h * rk + hi * rj - hj * ri + hk * r

        score = rotated_head_real * t + rotated_head_i * ti + rotated_head_j * tj + rotated_head_k * tk
        # regul1 = torch.mean(h**2) + torch.mean(hi**2) + torch.mean(hj**2) + torch.mean(hk**2) + \
        #         torch.mean(t**2) + torch.mean(ti**2) + torch.mean(tj**2) + torch.mean(tk**2)

        # regul2 = torch.mean(r**2) + torch.mean(ri**2) + torch.mean(rj**2) + torch.mean(rk**2)

        score = torch.sum(score, -1)
        return score
        # TODO: check here
        # score_r = rotated_head_real * t
        # score_i = rotated_head_i * ti
        # score_j = rotated_head_j * tj
        # score_k = rotated_head_k * tk

        # score = torch.stack([score_r, score_i, score_j, score_k], dim=0)
        # score = score.norm(dim=0)
        # return score.sum(dim=2)

    def TransQuatE(self, head, rotator_head, rotator_tail, translation, tail):
        head, head_i, head_j, head_k = torch.chunk(head, 4, dim=2)
        tail, tail_i, tail_j, tail_k = torch.chunk(tail, 4, dim=2)

        tail_i, tail_j, tail_k = -tail_i, -tail_j, -tail_k

        tran, tran_i, tran_j, tran_k = torch.chunk(translation, 4, dim=2)  # translations
        rot_h, rot_hi, rot_hj, rot_hk = self.normalize_quaternion(rotator_head)
        rot_t, rot_ti, rot_tj, rot_tk = self.normalize_quaternion(rotator_tail)

        rotated_head_real = head * rot_h - head_i * rot_hi - head_j * rot_hj - head_k * rot_hk
        rotated_head_i = head * rot_hi + head_i * rot_h + head_j * rot_hk - head_k * rot_hj
        rotated_head_j = head * rot_hj - head_i * rot_hk + head_j * rot_h + head_k * rot_hi
        rotated_head_k = head * rot_hk + head_i * rot_hj - head_j * rot_hi + head_k * rot_h

        rotated_tail_real = tail * rot_t - tail_i * rot_ti - tail_j * rot_tj - tail_k * rot_tk
        rotated_tail_i = tail * rot_ti + tail_i * rot_t + tail_j * rot_tk - tail_k * rot_tj
        rotated_tail_j = tail * rot_tj - tail_i * rot_hk + tail_j * rot_t + tail_k * rot_ti
        rotated_tail_k = tail * rot_tk + tail_i * rot_tj - tail_j * rot_ti + tail_k * rot_t

        score_r = rotated_head_real + tran - rotated_tail_real
        score_i = rotated_head_i + tran_i - rotated_tail_i
        score_j = rotated_head_j + tran_j - rotated_tail_j
        score_k = rotated_head_k + tran_k - rotated_tail_k

        score = torch.stack([score_r, score_i, score_j, score_k], dim=0)
        score = score.norm(dim=0)
        return score.sum(dim=2)

    def TransQuatEReg(self, head, rotator_head, mapping_regularizer, translation, tail):
        head_real, head_i, head_j, head_k = torch.chunk(head, 4, dim=2)
        # print("head_real", head_real.shape)
        rot_h, rot_hi, rot_hj, rot_hk = self.normalize_quaternion(rotator_head)  # relationdan gelen bir rotation degree
        # print("rot_h", rot_h.shape)

        # rotating head in 4 dim --- checked
        rotated_head_real = head_real * rot_h - head_i * rot_hi - head_j * rot_hj - head_k * rot_hk
        # print("rotated_head_real", rotated_head_real.shape)
        # print("head_real * rot_h", (head_real * rot_h).shape)
        rotated_head_i = head_real * rot_hi + head_i * rot_h + head_j * rot_hk - head_k * rot_hj
        rotated_head_j = head_real * rot_hj - head_i * rot_hk + head_j * rot_h + head_k * rot_hi
        rotated_head_k = head_real * rot_hk + head_i * rot_hj - head_j * rot_hi + head_k * rot_h

        # now translating head
        tran_real, tran_i, tran_j, tran_k = torch.chunk(translation, 4, dim=2)  # by relation
        # print("tran_real", tran_real.shape)
        translated_head_real = rotated_head_real + tran_real
        # print("translated_head_real = rotated_head_real + tran_real", translated_head_real.shape)
        translated_head_i = rotated_head_i + tran_i
        translated_head_j = rotated_head_j + tran_j
        translated_head_k = rotated_head_k + tran_k
        # -----------------------------------------------------#
        # mapping_regularizer_real, mapping_regularizer_i, mapping_regularizer_j, mapping_regularizer_k = \
        #    torch.chunk(mapping_regularizer, 4, dim=2)
        # TODO:
        mapping_regularizer_real, mapping_regularizer_i, mapping_regularizer_j, mapping_regularizer_k = self.normalize_quaternion(
            mapping_regularizer)
        # print("mapping_regularizer_real", mapping_regularizer_real.shape)

        tail_real, tail_i, tail_j, tail_k = torch.chunk(tail, 4, dim=2)

        # rotating mapping_regularizer in 4 dim by tail
        # --- checked
        rotated_mapping_regularizer_real = mapping_regularizer_real * tail_real - mapping_regularizer_i * tail_i - mapping_regularizer_j * tail_j - mapping_regularizer_k * tail_k
        # print("mapping_regularizer_real * tail_real", (mapping_regularizer_real * tail_real).shape)
        rotated_mapping_regularizer_i = mapping_regularizer_real * tail_i + mapping_regularizer_i * tail_real + mapping_regularizer_j * tail_k - mapping_regularizer_k * tail_j
        rotated_mapping_regularizer_j = mapping_regularizer_real * tail_j - mapping_regularizer_i * tail_k + mapping_regularizer_j * tail_real + mapping_regularizer_k * tail_i
        rotated_mapping_regularizer_k = mapping_regularizer_real * tail_k + mapping_regularizer_i * tail_j - mapping_regularizer_j * tail_i + mapping_regularizer_k * tail_real

        score_r = translated_head_real - rotated_mapping_regularizer_real
        # print("score_r", score_r.shape)
        score_i = translated_head_i - rotated_mapping_regularizer_i
        score_j = translated_head_j - rotated_mapping_regularizer_j
        score_k = translated_head_k - rotated_mapping_regularizer_k

        score = torch.stack([score_r, score_i, score_j, score_k], dim=0)
        # print("score stack", score.shape)
        score = score.norm(dim=0)

        return score.sum(dim=2)

    def QuatEVersor(self, head, rotator_head, rotator_tail, translation, tail):

        s_head, x_head, y_head, z_head = torch.chunk(head, 4, dim=2)
        s_head = torch.tensor([0], dtype=torch.float32).cuda() * s_head

        s_tail, x_tail, y_tail, z_tail = torch.chunk(tail, 4, dim=2)
        s_tail = torch.tensor([0], dtype=torch.float32).cuda() * s_tail

        relation_s, relation_x, relation_y, relation_z = torch.chunk(translation, 4, dim=2)
        l_relation_s, l_relation_x, l_relation_y, l_relation_z = torch.chunk(rotator_head, 4, dim=2)
        r_relation_s, r_relation_x, r_relation_y, r_relation_z = torch.chunk(rotator_tail, 4, dim=2)

        l_relation_x_c = - l_relation_x
        l_relation_y_c = - l_relation_y
        l_relation_z_c = - l_relation_z
        r_relation_x_c = - r_relation_x
        r_relation_y_c = - r_relation_y
        r_relation_z_c = - r_relation_z

        d = torch.stack([l_relation_s, l_relation_x, l_relation_y, l_relation_z], dim=2)
        d1 = d.norm(dim=3)

        denominator_l = torch.sqrt(l_relation_s ** 2 + l_relation_x ** 2 + l_relation_y ** 2 + l_relation_z ** 2)
        denominator_r = torch.sqrt(r_relation_s ** 2 + r_relation_x ** 2 + r_relation_y ** 2 + r_relation_z ** 2)
        denominator_h = torch.sqrt(s_head ** 2 + x_head ** 2 + y_head ** 2 + z_head ** 2)
        denominator_t = torch.sqrt(s_tail ** 2 + x_tail ** 2 + y_tail ** 2 + z_tail ** 2)

        s_head = s_head / (denominator_l * denominator_l)
        x_head = x_head / (denominator_l * denominator_l)
        y_head = y_head / (denominator_l * denominator_l)
        z_head = z_head / (denominator_l * denominator_l)
        s_tail = s_tail / (denominator_r * denominator_r)
        x_tail = x_tail / (denominator_r * denominator_r)
        y_tail = y_tail / (denominator_r * denominator_r)
        z_tail = z_tail / (denominator_r * denominator_r)

        ts_c = s_tail * r_relation_s - x_tail * r_relation_x_c - y_tail * r_relation_y_c - z_tail * r_relation_z_c
        tx_c = s_tail * r_relation_x_c + r_relation_s * x_tail + y_tail * r_relation_z_c - r_relation_y_c * z_tail
        ty_c = s_tail * r_relation_y_c + r_relation_s * y_tail + z_tail * r_relation_x_c - r_relation_z_c * x_tail
        tz_c = s_tail * r_relation_z_c + r_relation_s * z_tail + x_tail * r_relation_y_c - r_relation_x_c * y_tail

        ts = ts_c * r_relation_s - tx_c * r_relation_x - ty_c * r_relation_y - tz_c * r_relation_z
        tx = tx_c * r_relation_s + r_relation_x * ts_c + tz_c * r_relation_y - r_relation_z * ty_c
        ty = ty_c * r_relation_s + r_relation_y * ts_c + tx_c * r_relation_z - r_relation_x * tz_c
        tz = tz_c * r_relation_s + r_relation_z * ts_c + ty_c * r_relation_x - r_relation_y * tx_c

        hs_c = s_head * l_relation_s - x_head * l_relation_x_c - y_head * l_relation_y_c - z_head * l_relation_z_c
        hx_c = s_head * l_relation_x_c + l_relation_s * x_head + y_head * l_relation_z_c - l_relation_y_c * z_head
        hy_c = s_head * l_relation_y_c + l_relation_s * y_head + z_head * l_relation_x_c - l_relation_z_c * x_head
        hz_c = s_head * l_relation_z_c + l_relation_s * z_head + x_head * l_relation_y_c - l_relation_x_c * y_head

        hs = hs_c * l_relation_s - hx_c * l_relation_x - hy_c * l_relation_y - hz_c * l_relation_z
        hx = hx_c * l_relation_s + l_relation_x * hs_c + hz_c * l_relation_y - l_relation_z * hy_c
        hy = hy_c * l_relation_s + l_relation_y * hs_c + hx_c * l_relation_z - l_relation_x * hz_c
        hz = hz_c * l_relation_s + l_relation_z * hs_c + hy_c * l_relation_x - l_relation_y * hx_c

        s = hs + relation_s - ts
        x = hx + relation_x - tx
        y = hy + relation_y - ty
        z = hz + relation_z - tz

        score = torch.stack([s, x, y, z], dim=0)
        score = score.norm(dim=0)
        score = self.gamma.item() - score.sum(dim=2)

        return score

    def l2_regularizer2(self):
        regul = self.entity_embedding.norm() + self.relation_embedding.norm()
        regul = regul + self.rotator_head.norm()
        return regul

    def l2_regularizer(self):
        ent_re, ent_i, ent_j, ent_k = torch.chunk(self.entity_embedding, 4, dim=1)
        tr_re, tr_i, tr_j, tr_k = torch.chunk(self.relation_embedding, 4, dim=1)
        rot_re, rot_i, rot_j, rot_k = torch.chunk(self.rotator_head, 4, dim=1)
        term1 = torch.mean(ent_re ** 2) + torch.mean(ent_i ** 2) + torch.mean(ent_j ** 2) + torch.mean(ent_k ** 2)
        temp2 = torch.mean(tr_re ** 2) + torch.mean(tr_j ** 2) + torch.mean(tr_k ** 2) + torch.mean(tr_i ** 2)
        term3 = torch.mean(rot_re ** 2) + torch.mean(rot_i ** 2) + torch.mean(rot_j ** 2) + torch.mean(rot_k ** 2)
        l2_reg = term1 + temp2 + term3
        '''
        l2_reg = self.entity_embedding.norm()
        print('l2 reg at start - ', l2_reg.item())
        for name, data in self.named_parameters():
            if name in ['relation_embedding', 'rotator_head', 'rotator_tail']:
                #l2_reg = l2_reg + data.norm()
                print('data norm is ', data.norm().item())
                print('updated l2 reg - ', l2_reg.item())
                tmp = torch.sum(data**2)
                print('summed up squared data is ', tmp.item())
        print('final L2 reg term is ', l2_reg.item())
        '''
        return l2_reg

    # ---------------------------------------------------------------------------
    def bce_logits_loss(self, positive_score, negative_score):
        criterion = nn.BCEWithLogitsLoss(reduction='sum')
        positive_score = positive_score.squeeze()
        negative_score = negative_score.view(-1)
        total = positive_score.numel() + negative_score.numel()
        positive_loss = criterion(positive_score, torch.ones(positive_score.shape).cuda())
        negative_loss = criterion(negative_score, torch.zeros(negative_score.shape).cuda())
        loss = (positive_loss + negative_loss) / total
        return positive_loss / positive_score.numel(), negative_loss / negative_score.numel(), loss

    def ruge_loss(self, positive_score, negative_score, unlabeled_scores, soft_labels):
        # TODO: add adversarial temperature??
        positive_loss = self.criterion(positive_score, torch.ones(positive_score.shape).cuda())
        negative_loss = self.criterion(negative_score, torch.zeros(negative_score.shape).cuda())
        total_labeled = positive_score.numel() + negative_score.numel()

        unlabeled_loss = self.criterion(unlabeled_scores.squeeze(), soft_labels.detach())

        l2 = self.l2_regularizer()
        l2 = self.entity_embedding.norm() + self.relation_embedding.norm()

        loss = labeled_loss / total_labeled + unlabeled_loss.mean() + 0.0002 * l2

        loss = (positive_loss + negative_loss) / total_labeled + unlabeled_loss / unlabeled_scores.numel()
        return positive_loss / positive_score.numel(), negative_loss / negative_score.numel(), unlabeled_loss / unlabeled_scores.numel(), loss

    def quate_loss(self, positive_score, negative_score, subsampling_weight, args):
        negative_score = negative_score.view(1, -1).squeeze()
        positive_score = positive_score.squeeze()
        # print('positive score - ', positive_score)
        # print('negative score - ', negative_score)

        # regul1 = torch.mean(h**2) + torch.mean(hi**2) + torch.mean(hj**2) + torch.mean(hk**2) + \
        #         torch.mean(t**2) + torch.mean(ti**2) + torch.mean(tj**2) + torch.mean(tk**2)

        # regul2 = torch.mean(r**2) + torch.mean(ri**2) + torch.mean(rj**2) + torch.mean(rk**2)

        negative_loss = torch.mean(self.criterion(-negative_score))
        positive_loss = torch.mean(self.criterion(positive_score))

        regul1 = self.entity_embedding.norm(p=1) ** 2
        loss = positive_loss + negative_loss

        return positive_loss, negative_loss, loss

    def rotate_loss(self, positive_score, negative_score, subsampling_weight, args):
        # if self.model_name != 'ComplEx' and self.model_name != 'QuatE':
        if self.model_name != 'ComplEx' and self.model_name != 'QuatE':
            negative_score = self.gamma.item() - negative_score
            positive_score = self.gamma.item() - positive_score
        # TODO: quate ise regularizerla topla burda test yap sonra
        if args.negative_adversarial_sampling:
            # In self-adversarial sampling, we do not apply back-propagation on the sampling weight
            negative_score = (F.softmax(negative_score * args.adversarial_temperature, dim=1).detach()
                              * F.logsigmoid(-negative_score)).sum(dim=1)
        else:
            negative_score = F.logsigmoid(-negative_score).mean(dim=1)

        positive_score = F.logsigmoid(positive_score).squeeze(dim=1)

        if args.uni_weight:
            positive_sample_loss = - positive_score.mean()
            negative_sample_loss = - negative_score.mean()
        else:
            positive_sample_loss = - (subsampling_weight * positive_score).sum() / subsampling_weight.sum()
            negative_sample_loss = - (subsampling_weight * negative_score).sum() / subsampling_weight.sum()

        loss = (positive_sample_loss + negative_sample_loss) / 2

        if self.model_name == 'QuatE':
            # Use L3 regularization for ComplEx and DistMult
            regularization = 0.000005 * (
                    self.entity_embedding.norm(p=2) ** 2 +
                    self.relation_embedding.norm(p=2).norm(p=2) ** 2
            )
            loss = loss + regularization
            regularization_log = {'regularization': regularization.item()}

        return positive_sample_loss, negative_sample_loss, loss

    def custom_loss(self, positive_score, negative_score, subsampling_weight, args):
        negative_score = self.gamma2 - negative_score  # model.gamma - negative_score

        if args.negative_adversarial_sampling:
            # In self-adversarial sampling, we do not apply back-propagation on the sampling weight
            negative_score = (F.softmax(negative_score * args.adversarial_temperature, dim=1).detach()
                              * F.relu(negative_score)).sum(dim=1)
        else:
            negative_score = F.relu(negative_score).mean(dim=1)

        positive_score = positive_score - self.gamma1
        positive_score = F.relu(positive_score).squeeze(dim=1)

        if args.uni_weight:
            positive_sample_loss = positive_score.mean()
            negative_sample_loss = negative_score.mean()
        else:
            positive_sample_loss = (subsampling_weight * positive_score).sum() / subsampling_weight.sum()
            negative_sample_loss = (subsampling_weight * negative_score).sum() / subsampling_weight.sum()

        loss = (positive_sample_loss + negative_sample_loss) / 2
        return positive_sample_loss, negative_sample_loss, loss

    def uncertain_loss(self, positive_score, negative_score, subsampling_weight, args):

        if self.model_name != 'ComplEx':
            negative_score = self.gamma.item() - negative_score
            positive_score = positive_score - self.gamma.item()
        else:
            positive_score = -positive_score

        xi = self.xi[self.idx].squeeze(2)
        xi_neg = self.xi_neg[self.idx].squeeze(2)
        xi1 = xi_neg.repeat(1, negative_score.size()[1])

        if args.negative_adversarial_sampling:
            # In self-adversarial sampling, we do not apply back-propagation on the sampling weight
            negative_score = (F.softmax(negative_score * args.adversarial_temperature, dim=1).detach()
                              * F.softplus(negative_score + xi1 ** 2)).sum(dim=1)
        else:
            negative_score = F.softplus(negative_score + xi1 ** 2).mean(dim=1)

        positive_score = F.softplus(positive_score + xi ** 2).squeeze(dim=1)

        temp_pos = torch.exp(-self.alpha * xi ** 2)
        temp_neg = torch.exp(-self.alpha * xi1 ** 2)

        exp = torch.tensor([1000.05]).cuda().float() * torch.sum(temp_pos) \
              + torch.tensor([10.05]).cuda().float() * torch.sum(temp_neg)

        if args.uni_weight:
            positive_sample_loss = positive_score.mean()
            negative_sample_loss = negative_score.mean()
        else:
            positive_sample_loss = (subsampling_weight * positive_score).sum() / subsampling_weight.sum()
            negative_sample_loss = (subsampling_weight * negative_score).sum() / subsampling_weight.sum()

        loss = exp + (positive_sample_loss + negative_sample_loss) / 2
        # print(self.half_margin)

        return positive_sample_loss, negative_sample_loss, loss

    def Limit_Loss(self, positive_score, negative_score, subsampling_weight, args):
        temploss = torch.relu(positive_score + self.gamma - negative_score)  # + self.lda*(positive_score-self.gamma2))
        adv = 1 - F.softmax(negative_score * args.adversarial_temperature, dim=1).detach()
        if args.negative_adversarial_sampling:
            # In self-adversarial sampling, we do not apply back-propagation on the sampling weight
            temploss = (adv * temploss).sum(dim=1)
        else:
            temploss = temploss.mean(dim=1)

        if args.uni_weight:
            positive_sample_loss = positive_score.mean()
            negative_sample_loss = negative_score.mean()
        else:
            positive_sample_loss = positive_score.mean()
            negative_sample_loss = negative_score.mean()

        # yy = torch.tensor([1.0], dtype=torch.float).cuda()
        # mrg = self.gamma.type(torch.cuda.DoubleTensor)
        # criterion = nn.MarginRankingLoss(mrg).cuda()
        # loss = criterion(positive_score, negative_score, yy).cuda()
        loss = torch.mean(temploss)
        # print(loss)
        # y = np.repeat([-1], repeats=positive_score.shape[0])
        # y = torch.tensor(y, dtype=torch.float).cuda()
        # criterion = nn.MarginRankingLoss(
        #     margin=10.0,
        # )
        # loss = nn.MarginRankingLoss(margin=self.gamma.item())(positive_score, negative_score, yy)
        # print(self.gamma.item())
        # exit()
        return positive_sample_loss, negative_sample_loss, loss

    def adaptive_margin_loss(self, positive_score, negative_score, subsampling_weight, args):
        if self.model_name != 'ComplEx':
            negative_score = self.gamma.item() - negative_score
            positive_score = positive_score - self.gamma.item()
        else:
            positive_score = -positive_score
        if args.negative_adversarial_sampling:
            # In self-adversarial sampling, we do not apply back-propagation on the sampling weight
            negative_score = (F.softmax(negative_score * args.adversarial_temperature, dim=1).detach()
                              * F.relu(negative_score + self.margin ** 2)).sum(dim=1)
        else:
            negative_score = F.relu(negative_score + self.margin ** 2).mean(dim=1)

        positive_score = F.relu(positive_score + self.margin ** 2).squeeze(dim=1)

        if args.uni_weight:
            positive_sample_loss = positive_score.mean()
            negative_sample_loss = negative_score.mean()
        else:
            positive_sample_loss = (subsampling_weight * positive_score).sum() / subsampling_weight.sum()
            negative_sample_loss = (subsampling_weight * negative_score).sum() / subsampling_weight.sum()

        loss = self.lambda1 * torch.exp(-self.sigma * self.margin ** 2) + (
                positive_sample_loss + negative_sample_loss) / 2
        return positive_sample_loss, negative_sample_loss, loss

    # ------------------------------------------
    def predict_soft_labels(self, rules, use_cuda):
        longTensor = torch.cuda.LongTensor if use_cuda else torch.LongTensor
        floatTensor = torch.cuda.FloatTensor if use_cuda else torch.FloatTensor

        labeled = [rule.premise for rule in rules]
        unlabeled = [rule.conclusion for rule in rules]
        conf = [rule.conf for rule in rules]
        labeled = longTensor(labeled)
        unlabeled = longTensor(unlabeled)
        conf = floatTensor(conf)

        labeled_scores = self.forward(labeled)
        labeled_scores = labeled_scores.detach()
        labeled_scores = torch.sigmoid(labeled_scores).squeeze()
        unlabeled_scores = self.forward(unlabeled)
        unlabeled_scores_sigmoid = torch.sigmoid(unlabeled_scores).squeeze()

        soft_labels = unlabeled_scores_sigmoid + self.ruge_rule_penalty * conf * labeled_scores
        soft_labels = torch.clamp(soft_labels, min=0, max=1)
        return unlabeled_scores, soft_labels

    # ------------------ LOSS FUNCTIONS FOR GROUNDINGS ----------------------------
    @staticmethod
    def inverse_loss(model, groundings, longTensor):
        sample_premise = torch.index_select(groundings, 1, longTensor([0, 2, 1]))
        sample_conclusion = torch.index_select(groundings, 1, longTensor([1, 3, 0]))
        scores_premise = model(model.idx, sample_premise)
        scores_concl = model(model.idx, sample_conclusion)
        # difference = scores_premise - scores_concl
        # return F.relu(difference + model.epsilon_inv)
        return F.relu(scores_concl - scores_premise + model.epsilon_inv)  # new version
        # return F.relu(scores_premise - scores_concl + model.epsilon_inv)    # old version (reverse)

    @staticmethod
    def implication_loss(model, groundings, longTensor):
        sample_premise = torch.index_select(groundings, 1, longTensor([0, 2, 1]))
        sample_conclusion = torch.index_select(groundings, 1, longTensor([0, 3, 1]))
        scores_premise = model(sample_premise)
        scores_conclusion = model(sample_conclusion)
        # using adaptive margin loss as score
        score = scores_conclusion - scores_premise + model.epsilon_impl
        score = F.relu(score).mean(dim=1)
        return score

        # return F.relu(scores_conclusion - scores_premise + model.epsilon_impl)
        # score = scores_premise - scores_conclusion + model.epsilon_impl
        # return F.relu(score)

    @staticmethod
    def equality_loss(model, groundings, longTensor):
        sample_premise = torch.index_select(groundings, 1, longTensor([0, 2, 1]))
        sample_conclusion = torch.index_select(groundings, 1, longTensor([0, 3, 1]))
        scores_premise = model(sample_premise)
        scores_conclusion = model(sample_conclusion)
        loss = scores_premise - scores_conclusion - model.epsilon_eq
        # return torch.norm(difference - model.epsilon_eq, p = 1)
        # loss = torch.norm(difference, p = 1) - model.epsilon_eq
        # return F.relu(loss)
        # loss = scores_premise - scores_conclusion - model.epsilon_eq
        return torch.norm(loss, p=1)

    @staticmethod
    def symmetry_loss(model, groundings, longTensor):
        sample_premise = torch.index_select(groundings, 1, longTensor([0, 2, 1]))
        sample_conclusion = torch.index_select(groundings, 1, longTensor([1, 2, 0]))
        scores_premise = model(model.idx, sample_premise)
        scores_conclusion = model(model.idx, sample_conclusion)
        loss = scores_premise - scores_conclusion - model.epsilon_sym
        # loss = torch.norm(difference, p = 1) - model.epsilon_sym
        # return F.relu(loss)
        # loss = scores_premise - scores_conclusion - model.epsilon_sym
        return torch.norm(loss, p=1)

    @staticmethod
    def ruge_unlabeled_loss(model, groundings, use_cuda):
        unlabeled_scores, soft_labels = model.predict_soft_labels(groundings, use_cuda)
        unlabeled_scores = unlabeled_scores.squeeze()
        soft_labels = soft_labels.detach()
        softs_notones = soft_labels != 1
        softs_notzeros = soft_labels != 0
        softs = torch.sum(softs_notones == softs_notzeros).item()
        if softs != 0:
            print("FINALLY FOUND {} SOFT LABELS".format(softs))
        criterion = nn.BCEWithLogitsLoss()
        unlabeled_loss = criterion(unlabeled_scores, soft_labels.detach())
        return unlabeled_loss

    @staticmethod
    def adversarial_loss(adv_model, kge_model):
        # compute inconsistnecy loss
        conclusion, premise, conj = adv_model(kge_model, detach=False)  # clause scores
        loss = adv_model.adversarial_loss(conclusion, premise, conj, reverse=True)

        return loss

    # ------------------------------------------------------------------------------
    @staticmethod
    def check_nans(model):
        # print("in check nan")
        nan1 = nan2 = nan3 = nan4 = 0
        nan1 = torch.isnan(model.entity_embedding.grad).sum()
        nan2 = torch.isnan(model.relation_embedding.grad).sum()
        if model.model_name in ['TransRotatE', 'TransQuatE']:
            nan3 = torch.isnan(model.rotator_head.grad).sum()
            nan4 = torch.isnan(model.rotator_tail.grad).sum()
        if model.model_name in ['sTransRotatE', 'sTransQuatE']:
            nan3 = torch.isnan(model.rotator_head.grad).sum()
        if model.model_name in ['TransQuatEReg']:
            nan3 = torch.isnan(model.rotator_head.grad).sum()
            nan4 = torch.isnan(model.mapping_regularizer.grad).sum()

        if nan1 != 0: print(nan1.item(), ' nan vals in entity grads')
        if nan2 != 0: print(nan2.item(), ' nan vals in relation  grad')
        if nan3 != 0: print(nan3.item(), ' nan vals in rotator')
        if nan4 != 0: print(nan4.item(), ' nan vals in rotator/mapping reg')
        if nan1 + nan2 + nan3 + nan4 != 0: exit()

    @staticmethod
    def rule_train_step(model, groundings, use_cuda):
        loss_groundings = {
            'inverse': model.inverse_loss,
            'implication': model.implication_loss,
            'equality': model.equality_loss,
            'symmetry': model.symmetry_loss
        }

        loss_rules = 0
        longTensor = torch.cuda.LongTensor if use_cuda else torch.LongTensor
        rules_log = {}
        for rule_type, grounds in groundings.items():
            if use_cuda:
                grounds = grounds.cuda()
            curr_rule_loss = loss_groundings[rule_type](model, grounds, longTensor)
            curr_rule_loss = curr_rule_loss.mean()
            loss_rules += model.rule_weight[rule_type] * curr_rule_loss
            rules_log[rule_type + ' loss'] = curr_rule_loss.item()

        return loss_rules, rules_log

    @staticmethod
    def train_step(model, adv_model, optimizer, train_iterator, args, rules=None):
        '''
        A single train step. Apply back-propation and return the loss
        '''
        # project

        if args.project or args.adversarial:
            with torch.no_grad():
                model.relation_embedding.data = F.normalize(model.relation_embedding, p=2, dim=1)
                model.entity_embedding.data.copy_(torch.clamp(model.entity_embedding, min=0, max=1))

        model.train()

        optimizer.zero_grad()

        adv_loss = torch.cuda.FloatTensor([0]) if args.cuda else torch.FloatTensor([0])
        adv_log = {}
        if args.adversarial:
            adv_loss = model.adversarial_loss(adv_model, model)
            adv_log['inconsistency loss '] = adv_loss.item()

        # compute scores
        idx, positive_sample, negative_sample, subsampling_weight, mode = next(train_iterator)

        if args.cuda:
            positive_sample = positive_sample.cuda()
            negative_sample = negative_sample.cuda()
            subsampling_weight = subsampling_weight.cuda()

        negative_score = model(idx, (positive_sample, negative_sample), mode=mode)
        positive_score = model(idx, positive_sample)

        loss_args = {
            'positive_score': positive_score,
            'negative_score': negative_score,
        }
        if model.loss_name != 'bce':
            loss_args = {
                **loss_args,
                'subsampling_weight': subsampling_weight,
                'args': args
            }
        positive_loss, negative_loss, loss = model.Loss(**loss_args)

        if not model.inject and not args.ruge_inject:  # no rule injection; just take step
            loss.backward()
            model.check_nans(model)
            optimizer.step()
        # 3 options:
        # -- no rules at all
        # -- inject rules using regularizing terms
        # -- inject ruge rules
        unlabeled_log = {}
        rules_log = {}

        if args.ruge_inject:  # RUGE rule injection
            ruges = rules['ruge']
            ruge_loss = model.ruge_unlabeled_loss(model, rules['ruge'], args.cuda)
            unlabeled_log['unlabeled_loss'] = ruge_loss.item()
            loss = loss + ruge_loss
            loss.backward()
            model.check_nans(model)
            optimizer.step()

        elif rules:  # compute other rules
            # optimizer.zero_grad()
            rule_loss, rules_log = model.rule_train_step(model, rules, args.cuda)
            loss = loss + rule_loss
            if model.inject:
                loss.backward()
                model.check_nans(model)
                optimizer.step()

        log = {
            'positive_sample_loss': positive_loss,
            'negative_sample_loss': negative_loss,
            **adv_log,
            **unlabeled_log,
            # 'margin': model.margin.item(),
            **rules_log,
            'loss': loss.item()
        }
        return log

    @staticmethod
    def test_step(model, test_triples, all_true_triples, args):

        model.eval()

        rank_file_name = str(model.model_name) + "_gamma" + str(int(model.gamma.item())) + "_dim" + str(
            model.hidden_dim) + "_" + str(model.training_start_time)
        rank_file_loc = "data/SD2020/ranked_result_" + str(rank_file_name) + ".txt"

        # TODO: create new test function by removing head-bacthes
        # test_dataloader_head = DataLoader(
        #    TestDataset(
        #        test_triples,
        #        all_true_triples,
        #        args.nentity,
        #        args.nrelation,
        #        'head-batch'
        #    ),
        #    batch_size=args.test_batch_size,
        #    num_workers=max(1, args.cpu_num // 2),
        #    collate_fn=TestDataset.collate_fn
        # )

        test_dataloader_tail = DataLoader(
            TestDataset(
                test_triples,
                all_true_triples,
                args.nentity,
                args.nrelation,
                'tail-batch'
            ),
            batch_size=args.test_batch_size,
            num_workers=max(1, args.cpu_num // 2),
            collate_fn=TestDataset.collate_fn
        )

        # test_dataset_list = [test_dataloader_head, test_dataloader_tail]
        test_dataset_list = [test_dataloader_tail]

        logs = []

        step = 0
        total_steps = sum([len(dataset) for dataset in test_dataset_list])

        all_entities = model.load_entities(args)

        # h r t?

        all_relations = model.load_relations(args)

        with torch.no_grad():
            for test_dataset in test_dataset_list:
                for idx, positive_sample, negative_sample, filter_bias, mode in test_dataset:
                    # negative samples are corrupted heads or tails depends on mode
                    if args.cuda:
                        positive_sample = positive_sample.cuda()
                        # here
                        # print("positive sample(h_r_t)", positive_sample.size()) ([16, 3])
                        negative_sample = negative_sample.cuda()
                        # print("negative sample(tails)", negative_sample.size()) [16, 68907])
                        filter_bias = filter_bias.cuda()

                    batch_size = positive_sample.size(0)

                    score = model(idx, (positive_sample, negative_sample), mode)
                    # all the pos and neg samples score
                    if model.loss_name == 'quate':
                        score = -score
                    # TODO: for quate?
                    elif model.model_name != 'ComplEx' and model.model_name != 'QuatE':
                        score = model.gamma.item() - score
                    score += filter_bias
                    # Explicitly sort all the entities to ensure that there is no test exposure bias
                    argsort = torch.argsort(score, dim=1, descending=True)  # returns index after sort

                    if mode == 'head-batch':

                        positive_arg = positive_sample[:, 0]  # get the positive head
                        # positive_rel = positive_sample[:, 1]

                    elif mode == 'tail-batch':

                        positive_arg = positive_sample[:, 2]  # get the positive tail
                        # positive_rel = positive_sample[:, 1]
                    else:
                        raise ValueError('mode %s not supported' % mode)
                    ranked_triples = ""
                    for i in range(batch_size):
                        # Notice that argsort is not ranking
                        ranking = (argsort[i, :] == positive_arg[i]).nonzero()
                        assert ranking.size(0) == 1
                        # ranking + 1 is the true ranking used in evaluation metrics
                        ranking = 1 + ranking.item()

                        '''
                        if  args.do_test and  str(all_relations.at[int(positive_rel[
                                                        i].item()), "relations"]) == "hasTypes"  and mode == 'tail-batch' :
                            # if positive_rel[i].item() == 3:  # 3= hasTypes ranking <= 10 and and args.do_test
                            ranked_triples += str(ranking) + "\n"
                            entity = all_entities.at[int(positive_sample[:, 0][i].item()), "entities"]
                            ranked_triples += str(entity + "\t" + all_relations.at[
                                int(positive_rel[i].item()), "relations"] + "\tlist[ ]\n")
                            tsize = argsort[i, :].size(0)
                            data = argsort[i, :]
                            rank = argsort[i, :].nonzero()
                            ranked_triples += "list= ["
                            for j in range(tsize):
                                entity_name = all_entities.at[int(data[j].item()), "entities"]
                                if entity_name in ("Education", "Government", "Company", "Facility"):
                                    ranked_triples += str(
                                        str(entity_name) + "---" + str(int(rank[j].item()) + 1)) + "\n"
                            ranked_triples += "]\n"
                        '''
                        logs.append({
                            'MRR': 1.0 / ranking,
                            'MR': float(ranking),
                            'HITS@1': 1.0 if ranking <= 1 else 0.0,
                            'HITS@3': 1.0 if ranking <= 3 else 0.0,
                            'HITS@10': 1.0 if ranking <= 10 else 0.0,
                        })
                    if step % args.test_log_steps == 0:
                        logging.info('Evaluating the model... (%d/%d)' % (step, total_steps))
                    step += 1

                    # if args.do_test:
                    # print("Creating results of total :"+str(ranked_triples.count("\n"))+" lines")
                    # ranked_triple_file = io.open(rank_file_loc, "a+", encoding="utf-8")
                    # ranked_triple_file.write(ranked_triples)
                    # ranked_triple_file.close()
        metrics = {}
        for metric in logs[0].keys():
            metrics[metric] = sum([log[metric] for log in logs]) / len(logs)

        return metrics

    '''
    @staticmethod
    def test_step(model, test_triples, all_true_triples, args):

        #Evaluate the model on test or valid datasets


        model.eval()

        rank_file_name = str(model.model_name) +"_gamma"+ str(int(model.gamma.item()))+"_dim" + str(model.hidden_dim) +"_"+ str(model.training_start_time)
        rank_file_loc = "data/SD2020/ranked_result_" + str(rank_file_name)+".txt"

        if args.countries:
            # Countries S* datasets are evaluated on AUC-PR
            # Process test data for AUC-PR evaluation
            sample = list()
            y_true = list()
            for head, relation, tail in test_triples:
                for candidate_region in args.regions:
                    y_true.append(1 if candidate_region == tail else 0)
                    sample.append((head, relation, candidate_region))

            sample = torch.LongTensor(sample)
            if args.cuda:
                sample = sample.cuda()

            with torch.no_grad():
                y_score = model(sample).squeeze(1).cpu().numpy()

            y_true = np.array(y_true)

            # average_precision_score is the same as auc_pr
            auc_pr = average_precision_score(y_true, y_score)

            metrics = {'auc_pr': auc_pr}

        else:
            # Otherwise use standard (filtered) MRR, MR, HITS@1, HITS@3, and HITS@10 metrics
            # Prepare dataloader for evaluation


            #TODO: create new test function by removing head-bacthes
            test_dataloader_head = DataLoader(
                TestDataset(
                    test_triples,
                    all_true_triples,
                    args.nentity,
                    args.nrelation,
                    'head-batch'
                ),
                batch_size=args.test_batch_size,
                num_workers=max(1, args.cpu_num // 2),
                collate_fn=TestDataset.collate_fn
            )

            test_dataloader_tail = DataLoader(
                TestDataset(
                    test_triples,
                    all_true_triples,
                    args.nentity,
                    args.nrelation,
                    'tail-batch'
                ),
                batch_size=args.test_batch_size,
                num_workers=max(1, args.cpu_num // 2),
                collate_fn=TestDataset.collate_fn
            )

            test_dataset_list = [test_dataloader_head, test_dataloader_tail]

            logs = []

            step = 0
            total_steps = sum([len(dataset) for dataset in test_dataset_list])

            all_entities = model.load_entities(args)

            # h r t?

            all_relations = model.load_relations(args)

            with torch.no_grad():
                for test_dataset in test_dataset_list:
                    for idx, positive_sample, negative_sample, filter_bias, mode in test_dataset:
                        # negative samples are corrupted heads or tails depends on mode
                        if args.cuda:
                            positive_sample = positive_sample.cuda()
                            negative_sample = negative_sample.cuda()
                            filter_bias = filter_bias.cuda()

                        batch_size = positive_sample.size(0)

                        score = model(idx, (positive_sample, negative_sample), mode)
                        # all the pos and neg samples score
                        if model.loss_name == 'quate':
                            score = -score
                        elif model.model_name != 'ComplEx':
                            score = model.gamma.item() - score
                        score += filter_bias
                        # Explicitly sort all the entities to ensure that there is no test exposure bias
                        argsort = torch.argsort(score, dim=1, descending=True) #returns index after sort

                        if mode == 'head-batch':

                            positive_arg = positive_sample[:, 0]
                            positive_rel = positive_sample[:, 1]

                        elif mode == 'tail-batch':

                            positive_arg = positive_sample[:, 2]
                            positive_rel = positive_sample[:, 1]
                        else:
                            raise ValueError('mode %s not supported' % mode)
                        ranked_triples = ""
                        for i in range(batch_size):
                            # Notice that argsort is not ranking
                            ranking = (argsort[i, :] == positive_arg[i]).nonzero()
                            assert ranking.size(0) == 1
                            # ranking + 1 is the true ranking used in evaluation metrics
                            ranking = 1 + ranking.item()

                            if  args.do_test and  str(all_relations.at[int(positive_rel[
                                                            i].item()), "relations"]) == "hasTypes"  and mode == 'tail-batch' :
                                # if positive_rel[i].item() == 3:  # 3= hasTypes ranking <= 10 and and args.do_test
                                ranked_triples += str(ranking) + "\n"
                                entity = all_entities.at[int(positive_sample[:, 0][i].item()), "entities"]
                                ranked_triples += str(entity + "\t" + all_relations.at[
                                    int(positive_rel[i].item()), "relations"] + "\tlist[ ]\n")
                                tsize = argsort[i, :].size(0)
                                data = argsort[i, :]
                                rank = argsort[i, :].nonzero()
                                ranked_triples += "list= ["
                                for j in range(tsize):
                                    entity_name = all_entities.at[int(data[j].item()), "entities"]
                                    if entity_name in ("Education", "Government", "Company", "Facility"):
                                        ranked_triples += str(
                                            str(entity_name) + "---" + str(int(rank[j].item()) + 1)) + "\n"
                                ranked_triples += "]\n"

                            logs.append({
                                'MRR': 1.0 / ranking,
                                'MR': float(ranking),
                                'HITS@1': 1.0 if ranking <= 1 else 0.0,
                                'HITS@3': 1.0 if ranking <= 3 else 0.0,
                                'HITS@10': 1.0 if ranking <= 10 else 0.0,
                            })
                        if step % args.test_log_steps == 0:
                            logging.info('Evaluating the model... (%d/%d)' % (step, total_steps))
                        step += 1

                        if args.do_test:
                            print("Creating results of total :"+str(ranked_triples.count("\n"))+" lines")
                            ranked_triple_file = io.open(rank_file_loc, "a+", encoding="utf-8")
                            ranked_triple_file.write(ranked_triples)
                            ranked_triple_file.close()
            metrics = {}
            for metric in logs[0].keys():
                metrics[metric] = sum([log[metric] for log in logs]) / len(logs)

        return metrics
    '''#!/usr/bin/python3

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import logging
import datetime

import numpy as np

import torch
import torch.nn as nn
import torch.nn.functional as F

import numpy as np
from numpy.random import RandomState
import os
import io

from sklearn.metrics import average_precision_score

from torch.utils.data import DataLoader

from dataloader import TestDataset
import io
import pandas as pd

# regularization terms options
L2 = True
L1 = False
L2_COEFF = 0.00002

PROJECT_CUBE = False
PROJECT_SPHERE = False


class KGEModel(nn.Module):
    def __init__(self, model_name, nentity, nrelation, ntriples, hidden_dim, args):
        super(KGEModel, self).__init__()
        '''
        if L2:
            print('L2 regul with coeff ', L2_COEFF)
        if L1:
            print('L1 regul with coeff ', L2_COEFF)
        '''
        print("torch version:", torch.__version__)

        self.training_start_time = datetime.datetime.now().strftime('%Y%m%d_%H%M')
        self.model_name = model_name
        self.nentity = nentity
        self.nrelation = nrelation
        self.epsilon = 2.0
        self.lmbda = 0.1  # lambda for QuatE loss
        self.hidden_dim = hidden_dim
        self.idx = 1
        self.ruge_rule_penalty = 1
        self.alpha = 1000

        self.gamma = nn.Parameter(
            torch.Tensor([args.gamma]),
            requires_grad=False
        )

        self.embedding_range = nn.Parameter(
            torch.Tensor([(self.gamma.item() + self.epsilon) / hidden_dim]),
            requires_grad=False
        )

        #self.xi = nn.Parameter(torch.zeros(ntriples, 1))
        #nn.init.uniform_(
        #    tensor=self.xi,
        #    a=-0.1,
        #    b=0.1
        #)

        #self.xi_neg = nn.Parameter(torch.zeros(ntriples, 1))
        #nn.init.uniform_(
        #    tensor=self.xi_neg,
        #    a=-0.1,
        #    b=0.1
        #)

        ent_dim_mult, rel_dim_mult = self.compute_multipliers()

        # define entity and relation embeddings
        self.entity_dim = ent_dim_mult * hidden_dim
        self.relation_dim = rel_dim_mult * hidden_dim

        self.entity_embedding = nn.Parameter(torch.zeros(nentity, self.entity_dim))
        self.relation_embedding = nn.Parameter(torch.zeros(self.nrelation, self.relation_dim))
        self.initialize(self.entity_embedding, nentity, hidden_dim)
        self.initialize(self.relation_embedding, nrelation, hidden_dim)
        # setup multipliers
        if self.model_name in ['TransRotatE', 'TransQuatE', 'QuatEVersor']:
            mult = 4 if 'Quat' in self.model_name else 1
            self.rotator_head = nn.Parameter(torch.zeros(nrelation, mult * hidden_dim))
            self.rotator_tail = nn.Parameter(torch.zeros(nrelation, mult * hidden_dim))
            self.initialize(self.rotator_head, nrelation, hidden_dim)
            self.initialize(self.rotator_tail, nrelation, hidden_dim)

        if self.model_name == 'sTransRotatE' or self.model_name == 'sTransQuatE':
            self.rotator_head = nn.Parameter(torch.zeros(nrelation, hidden_dim))
            self.initialize(self.rotator_head, nrelation, hidden_dim)

        if self.model_name in ['TransQuatEReg']:
            mult = 4 if 'Quat' in self.model_name else 1
            self.rotator_head = nn.Parameter(torch.zeros(nrelation, mult * hidden_dim))
            self.mapping_regularizer = nn.Parameter(torch.zeros(nentity, mult * hidden_dim))
            self.initialize(self.rotator_head, nrelation, hidden_dim)
            self.initialize(self.mapping_regularizer, nentity, hidden_dim)


        # set rule info
        self.ruge = args.ruge

        self.epsilon_inv = .1
        self.epsilon_impl = .1
        self.epsilon_eq = 0
        self.epsilon_sym = 0
        self.inject = args.inject

        self.rule_weight = {
            'inverse': 2.0,
            'implication': 1.0,
            'symmetry': .01,
            'equality': .01,
            'ruge': 1
        }

        if model_name == 'pRotatE':
            self.modulus = nn.Parameter(torch.Tensor([[0.5 * self.embedding_range.item()]]))

        self.gamma1 = 23;
        self.gamma2 = 25

        # Do not forget to modify this line when you add a new model in the "forward" function
        if model_name not in ['TransE', 'DistMult', 'ComplEx', 'RotatE', 'pRotatE', 'TransRotatE', 'QuatE',
                              'TransQuatE', 'sTransRotatE', 'sTransQuatE', 'QuatEVersor', 'TransQuatEReg']:
            raise ValueError('model %s not supported' % model_name)

    def load_entities(self, args):
        data_path = args.data_path
        all_entities = pd.read_csv(os.path.join(data_path, 'entities.dict'), delimiter='\t', names=["id", "entities"],
                                   dtype=str)
        all_entities = pd.DataFrame(all_entities)

        return all_entities

    def load_relations(self, args):
        data_path = args\
            .data_path
        all_relations = pd.read_csv(os.path.join(data_path, 'relations.dict'), delimiter='\t',
                                    names=["id", "relations"],
                                    dtype=str)
        all_relations = pd.DataFrame(all_relations)
        return all_relations

    def compute_multipliers(self):
        if self.model_name == 'RotatE':
            return 2, 1
        if self.model_name in ['TransRotatE', 'ComplEx', 'DistMult', 'sTransRotatE']:
            return 2, 2
        if self.model_name in ['QuatE', 'TransQuatE', 'sTransQuatE', 'QuatEVersor', 'TransQuatEReg']:
            return 4, 4
        return 1, 1

    def initialize(self, tensor, in_features, out_features):
        if 'Quat' not in self.model_name:
            nn.init.uniform_(
                tensor=tensor,
                a=-self.embedding_range.item(),
                b=self.embedding_range.item()
            )

        else:  # use quaternion initialization
            fan_in = in_features
            fan_out = out_features

            s = 1. / np.sqrt(2 * in_features)

            rng = torch.random.manual_seed(42)

            # Generating randoms and purely imaginary quaternions :
            kernel_shape = (in_features, out_features)
            nweigths = in_features * out_features

            wi = torch.FloatTensor(nweigths).uniform_()
            wj = torch.FloatTensor(nweigths).uniform_()
            wk = torch.FloatTensor(nweigths).uniform_()

            # Purely imaginary quaternions unitary
            norms = torch.sqrt(wi ** 2 + wj ** 2 + wk ** 2) + 0.0001
            wi /= norms
            wj /= norms
            wk /= norms

            wi = wi.reshape(kernel_shape)
            wj = wj.reshape(kernel_shape)
            wk = wk.reshape(kernel_shape)

            modulus = torch.zeros(kernel_shape).uniform_(-s, s)
            phase = torch.zeros(kernel_shape).uniform_(-np.pi, np.pi)

            weight_r = modulus * torch.cos(phase)
            weight_i = modulus * wi * torch.sin(phase)
            weight_j = modulus * wj * torch.sin(phase)
            weight_k = modulus * wk * torch.sin(phase)

            tensor.data = torch.cat((weight_r, weight_i, weight_j, weight_k), dim=1)

    def set_loss(self, loss_name):
        self.loss_name = loss_name
        loss_fnc = {
            'rotate': self.rotate_loss,
            'custom': self.custom_loss,
            'adaptive_margin': self.adaptive_margin_loss,
            'quate': self.quate_loss,
            'ruge': self.ruge_loss,
            'bce': self.bce_logits_loss,  # ruge loss; to test models with and without ruge rule addition
            'uncertain_loss': self.uncertain_loss,
            'limit_loss': self.Limit_Loss
        }

        if loss_name == 'quate':
            self.criterion = nn.Softplus()

        if loss_name == 'limit_loss':
            self.lda = 0.01
            print(self.lda)

        if loss_name == 'adaptive_margin':
            self.margin = nn.Parameter(torch.FloatTensor([0.1]), requires_grad=True)

            self.lambda1 = 1.
            self.sigma = 1.01000100

        if loss_name == 'ruge':
            self.criterion = nn.BCEWithLogitsLoss(reduction='sum')
            self.ruge_rule_penalty = .01

        if loss_name not in loss_fnc:
            raise ValueError('model %s not supported' % loss_name)

        self.Loss = loss_fnc[loss_name]

    def select_relations(self, indices):
        relation_dict = {}
        if self.model_name in ['biRotatE', 'TransRotatE', 'TransQuatE', 'QuatEVersor']:
            relation_head = torch.index_select(
                self.rotator_head,
                dim=0,
                index=indices
            ).unsqueeze(1)

            relation_tail = torch.index_select(
                self.rotator_tail,
                dim=0,
                index=indices
            ).unsqueeze(1)
            relation_dict = {'rotator_head': relation_head, 'rotator_tail': relation_tail}
            # relation_list = [relation_head, relation_tail]

        if self.model_name in ['TransQuatEReg']:
            rotator_head = torch.index_select(
                self.rotator_head,
                dim=0,
                index=indices
            ).unsqueeze(1)
            #relation_dict = {'rotator_head': rotator_head}
            relation_dict['rotator_head'] = rotator_head
            # relation_list = [relation_head, relation_tail]

        if self.model_name == 'sTransRotatE' or self.model_name == 'sTransQuatE':
            relation_head = torch.index_select(
                self.rotator_head,
                dim=0,
                index=indices
            ).unsqueeze(1)
            # relation_list = [relation_head]
            relation_dict = {'rotator_head': relation_head}

        if self.model_name != 'biRotatE':
            relation = torch.index_select(
                self.relation_embedding,
                dim=0,
                index=indices
            ).unsqueeze(1)
            relation_dict['translation'] = relation
            # relation_list.append(relation)

        return relation_dict

    def entities_select(self, indices_heads, indices_tails):
        head = torch.index_select(
            self.entity_embedding,
            dim=0,
            index=indices_heads)

        tail = torch.index_select(
            self.entity_embedding,
            dim=0,
            index=indices_tails)

        if self.model_name in ['TransQuatEReg']:
            mapping_regularizer = torch.index_select(
                self.mapping_regularizer,
                dim=0,
                index=indices_heads) # TODO: checked

            #entity_dict = {'mapping_reqularizer': mapping_reqularizer, 'head': head, 'tail': tail}
            return head, tail, mapping_regularizer

        return head, tail

    def forward(self, idx, sample, mode='single'):
        '''
        Forward function that calculate the score of a batch of triples.
        In the 'single' mode, sample is a batch of triple.
        In the 'head-batch' or 'tail-batch' mode, sample consists two part.
        The first part is usually the positive sample.
        And the second part is the entities in the negative samples.
        Because negative samples and positive samples usually share two elements
        in their triple ((head, relation) or (relation, tail)).
        '''
        self.idx = idx

        relation_list = []

        if self.model_name in ['TransQuatEReg']:
            if mode == 'single':
                batch_size, negative_sample_size = sample.size(0), 1
                head, tail, mapping_regularizer = self.entities_select(sample[:, 0], sample[:, 2])
                head = head.unsqueeze(1)
                tail = tail.unsqueeze(1)
                mapping_regularizer = mapping_regularizer.unsqueeze(1)

                relation_dict = self.select_relations(sample[:, 1])

            elif mode == 'head-batch':
                tail_part, head_part = sample
                #tail part has positive triples  h,r,t
                #head_part has corrupted heads only h'X 10
                batch_size, negative_sample_size = head_part.size(0), head_part.size(1)

                head, tail, mapping_regularizer = self.entities_select(head_part.view(-1), tail_part[:, 2])
                head = head.view(batch_size, negative_sample_size, -1)
                tail = tail.unsqueeze(1)
                mapping_regularizer = mapping_regularizer.view(batch_size, negative_sample_size, -1)

                relation_dict = self.select_relations(tail_part[:, 1])

            elif mode == 'tail-batch':
                head_part, tail_part = sample
                # head_part has positive triples  h,r,t
                # tail_part has corrupted tails only h'X 10

                batch_size, negative_sample_size = tail_part.size(0), tail_part.size(1)
                head, tail, mapping_regularizer = self.entities_select(head_part[:, 0], tail_part.view(-1))
                head = head.unsqueeze(1)
                tail = tail.view(batch_size, negative_sample_size, -1)
                mapping_regularizer = mapping_regularizer.unsqueeze(1)

                relation_dict = self.select_relations(head_part[:, 1])

            else:
                raise ValueError('mode %s not supported' % mode)
            arg_dict = {
                'head': head,
                **relation_dict,
                'tail': tail,
                'mapping_regularizer': mapping_regularizer
            }
        else:
            if mode == 'single':
                batch_size, negative_sample_size = sample.size(0), 1
                head, tail = self.entities_select(sample[:, 0], sample[:, 2])
                head = head.unsqueeze(1)
                tail = tail.unsqueeze(1)

                relation_dict = self.select_relations(sample[:, 1])

            elif mode == 'head-batch':
                tail_part, head_part = sample
                batch_size, negative_sample_size = head_part.size(0), head_part.size(1)

                head, tail = self.entities_select(head_part.view(-1), tail_part[:, 2])
                head = head.view(batch_size, negative_sample_size, -1)
                tail = tail.unsqueeze(1)

                relation_dict = self.select_relations(tail_part[:, 1])

            elif mode == 'tail-batch':
                head_part, tail_part = sample
                batch_size, negative_sample_size = tail_part.size(0), tail_part.size(1)
                head, tail = self.entities_select(head_part[:, 0], tail_part.view(-1))
                head = head.unsqueeze(1)
                tail = tail.view(batch_size, negative_sample_size, -1)

                relation_dict = self.select_relations(head_part[:, 1])

            else:
                raise ValueError('mode %s not supported' % mode)
            arg_dict = {
                'head': head,
                **relation_dict,
                'tail': tail

            }
        # arg_list = [head] + relation_list + [tail]
        # arg_list += [mode] if self.model_name not in ['biRotatE', 'TransRotatE', 'TransQuatE', 'QuatE', 'sTransQuatE','sTransRotatE'] else []
        if self.model_name not in ['biRotatE', 'TransRotatE', 'TransQuatE', 'QuatE', 'sTransQuatE', 'sTransRotatE',
                                   'QuatEVersor', 'TransQuatEReg']:
            arg_dict['mode'] = mode
        score = self.compute_score(arg_dict)
        return score

    def compute_score(self, arg_dict):
        model_func = {
            'TransE': self.TransE,
            'DistMult': self.DistMult,
            'ComplEx': self.ComplEx,
            'RotatE': self.RotatE,
            'pRotatE': self.pRotatE,
            'biRotatE': self.biRotatE,
            'TransRotatE': self.TransRotatE,
            'QuatE': self.QuatE,
            'TransQuatE': self.TransQuatE,
            'sTransRotatE': self.HalfRotatE,
            'sTransQuatE': self.halfTransQuatE,
            'QuatEVersor': self.QuatEVersor,
            'TransQuatEReg': self.TransQuatEReg
        }

        score = model_func[self.model_name](**arg_dict)
        return score

    def TransE(self, head, translation, tail, mode):
        if mode == 'head-batch':
            score = head + (translation - tail)
        else:
            score = (head + translation) - tail
        score = torch.norm(score, p=1, dim=2)
        return score
        # return self.gamma.item() - score

    def DistMult(self, head, translation, tail, mode):
        if mode == 'head-batch':
            score = head * (translation * tail)
        else:
            score = (head * translation) * tail

        score = score.sum(dim=2)
        return score

    def ComplEx(self, head, translation, tail, mode):
        re_head, im_head = torch.chunk(head, 2, dim=2)
        re_relation, im_relation = torch.chunk(translation, 2, dim=2)
        re_tail, im_tail = torch.chunk(tail, 2, dim=2)
        '''if mode == 'head-batch':  # take complex conjugate of head
                                    score = re_head*re_relation*re_tail + \
                                            re_relation*im_head*im_tail - \
                                            im_relation*re_head*im_tail + \
                                            im_relation*im_head*re_tail
                                else:
                                    score = re_relation*re_head*re_tail + \
                                            re_relation*im_head*im_tail + \
                                            im_relation*re_head*im_tail - \
                                            im_relation*im_head*re_tail
                                score = score.sum(dim = 2)
                                return score'''

        if mode == 'head-batch':
            re_score = re_relation * re_tail + im_relation * im_tail
            im_score = re_relation * im_tail - im_relation * re_tail
            score = re_head * re_score + im_head * im_score
        else:
            re_score = re_head * re_relation - im_head * im_relation
            im_score = re_head * im_relation + im_head * re_relation
            score = re_score * re_tail + im_score * im_tail

        score = score.sum(dim=2)
        return score

    def RotatE(self, head, translation, tail, mode):
        pi = 3.14159265358979323846

        re_head, im_head = torch.chunk(head, 2, dim=2)
        re_tail, im_tail = torch.chunk(tail, 2, dim=2)

        # Make phases of relations uniformly distributed in [-pi, pi]

        '''phase_relation = relation/(self.embedding_range.item()/pi)

            re_relation = torch.cos(phase_relation)
            im_relation = torch.sin(phase_relation)
        '''
        re_relation, im_relation = self.extract_relations(translation)
        if mode == 'head-batch':
            re_score = re_relation * re_tail + im_relation * im_tail
            im_score = re_relation * im_tail - im_relation * re_tail
            re_score = re_score - re_head
            im_score = im_score - im_head
        else:
            re_score = re_head * re_relation - im_head * im_relation
            im_score = re_head * im_relation + im_head * re_relation
            re_score = re_score - re_tail
            im_score = im_score - im_tail

        score = torch.stack([re_score, im_score], dim=0)
        score = score.norm(dim=0)
        score = score.sum(dim=2)
        return score

    def extract_relations(self, *args):
        pi = 3.14159265358979323846

        split_relations = []
        for relation in args:
            phase_relation = relation / (self.embedding_range.item() / pi)
            re_relation = torch.cos(phase_relation)
            im_relation = torch.sin(phase_relation)
            split_relations.extend([re_relation, im_relation])
        return split_relations

    def TransRotatE(self, head, rotator_head, rotator_tail, translation, tail):
        re_head, im_head = torch.chunk(head, 2, dim=2)
        re_tail, im_tail = torch.chunk(tail, 2, dim=2)

        # multipliers
        re_relation_head, im_relation_head, re_relation_tail, im_relation_tail = self.extract_relations(
            rotator_head, rotator_tail)

        re_translation, im_translation = torch.chunk(translation, 2, dim=2)

        re_score_head = re_relation_head * re_head - im_relation_head * im_head
        im_score_head = re_relation_head * im_head + im_relation_head * re_head

        re_score_tail = re_relation_tail * re_tail + im_relation_tail * im_tail
        im_score_tail = re_relation_tail * im_tail - im_relation_tail * re_tail

        re_score = re_score_head + re_translation - re_score_tail
        im_score = im_score_head + re_translation - im_score_tail
        score = torch.stack([re_score, im_score], dim=0)
        score = torch.norm(score, p=1, dim=0)
        score = score.sum(dim=2)
        return score

    def HalfRotatE(self, head, rotator_head, translation, tail):
        re_head, im_head = torch.chunk(head, 2, dim=2)
        re_tail, im_tail = torch.chunk(tail, 2, dim=2)

        # multipliers
        re_relation_head, im_relation_head = self.extract_relations(rotator_head)

        re_translation, im_translation = torch.chunk(translation, 2, dim=2)

        re_score_head = re_relation_head * re_head - im_relation_head * im_head
        im_score_head = re_relation_head * im_head + im_relation_head * re_head

        # re_score_tail = re_relation_tail * re_tail + im_relation_tail * im_tail
        # im_score_tail = re_relation_tail * im_tail - im_relation_tail * re_tail

        re_score = re_score_head + re_translation - re_tail
        im_score = im_score_head + re_translation + im_tail
        score = torch.stack([re_score, im_score], dim=0)
        score = torch.norm(score, p=1, dim=0)
        score = score.sum(dim=2)
        return score

    def biRotatE(self, head, rotator_head, rotator_tail, tail):
        '''
        Using double rotate computation the score
        '''

        re_head, im_head = torch.chunk(head, 2, dim=2)
        re_tail, im_tail = torch.chunk(tail, 2, dim=2)

        re_relation_head, im_relation_head, re_relation_tail, im_relation_tail \
            = self.extract_relations(rotator_head, rotator_tail)
        # Make phases of relations uniformly distributed in [-pi, pi]

        re_score = re_relation_head * re_head - im_relation_head * im_head
        im_score = re_relation_head * im_head + im_relation_head * re_head

        re_score_tail = re_relation_tail * re_tail + im_relation_tail * im_tail
        im_score_tail = re_relation_tail * im_tail - im_relation_tail * re_tail

        re_score = re_score - re_score_tail
        im_score = im_score - im_score_tail
        score = torch.stack([re_score, im_score], dim=0)
        score = score.norm(dim=0)
        score = score.sum(dim=2)
        return score

    def pRotatE(self, head, translation, tail, mode):
        pi = 3.14159262358979323846

        # Make phases of entities and relations uniformly distributed in [-pi, pi]
        phase_head = head / (self.embedding_range.item() / pi)
        phase_relation = translation / (self.embedding_range.item() / pi)
        phase_tail = tail / (self.embedding_range.item() / pi)

        if mode == 'head-batch':
            score = phase_head + (phase_relation - phase_tail)
        else:
            score = (phase_head + phase_relation) - phase_tail

        score = torch.sin(score)
        score = torch.abs(score)

        score = self.gamma.item() - score.sum(dim=2) * self.modulus
        return score

    def normalize_quaternion(self, tensor):
        t, ti, tj, tk = torch.chunk(tensor, 4, dim=2)
        denom = torch.sqrt(t ** 2 + ti ** 2 + tj ** 2 + tk ** 2)
        t = t / denom
        ti = ti / denom
        tj = tj / denom
        tk = tk / denom
        return t, ti, tj, tk

    def halfTransQuatE(self, head, rotator_head, translation, tail):
        h, hi, hj, hk = torch.chunk(head, 4, dim=2)
        t, ti, tj, tk = torch.chunk(tail, 4, dim=2)

        ti, tj, tk = -ti, -tj, -tk
        tran, tran_i, tran_j, tran_k = torch.chunk(translation, 4, dim=2)  # translations
        rot_h, rot_hi, rot_hj, rot_hk = self.normalize_quaternion(rotator_head)

        # Rotate head
        rotated_head_real = h * rot_h - hi * rot_hi - hj * rot_hj - hk * rot_hk
        rotated_head_i = h * rot_hi + hi * rot_h + hj * rot_hk - hk * rot_hj
        rotated_head_j = h * rot_hj - hi * rot_hk + hj * rot_h + hk * rot_hi
        rotated_head_k = h * rot_hk + hi * rot_hj - hj * rot_hi + hk * rot_h

        # translate and score
        score_r = rotated_head_real + tran - t
        score_i = rotated_head_i + tran_i - ti
        score_j = rotated_head_j + tran_j - tk
        score_k = rotated_head_k + tran_k - tj

        score = torch.stack([score_r, score_i, score_j, score_k], dim=0)
        score = torch.norm(score, dim=0).sum(dim=2)

        return score

    def QuatE(self, head, translation, tail):
        h, hi, hj, hk = torch.chunk(head, 4, dim=2)
        t, ti, tj, tk = torch.chunk(tail, 4, dim=2)

        r, ri, rj, rk = self.normalize_quaternion(translation)

        rotated_head_real = h * r - hi * ri - hj * rj - hk * rk
        rotated_head_i = h * ri + hi * r + hj * rk - hk * rj
        rotated_head_j = h * rj - hi * rk + hj * r + hk * ri
        rotated_head_k = h * rk + hi * rj - hj * ri + hk * r

        score = rotated_head_real * t + rotated_head_i * ti + rotated_head_j * tj + rotated_head_k * tk
        # regul1 = torch.mean(h**2) + torch.mean(hi**2) + torch.mean(hj**2) + torch.mean(hk**2) + \
        #         torch.mean(t**2) + torch.mean(ti**2) + torch.mean(tj**2) + torch.mean(tk**2)

        # regul2 = torch.mean(r**2) + torch.mean(ri**2) + torch.mean(rj**2) + torch.mean(rk**2)

        score = torch.sum(score, -1)
        return score
        # TODO: check here
        #score_r = rotated_head_real * t
        #score_i = rotated_head_i * ti
        #score_j = rotated_head_j * tj
        #score_k = rotated_head_k * tk

        #score = torch.stack([score_r, score_i, score_j, score_k], dim=0)
        #score = score.norm(dim=0)
        #return score.sum(dim=2)

    def TransQuatE(self, head, rotator_head, rotator_tail, translation, tail):
        head, head_i, head_j, head_k = torch.chunk(head, 4, dim=2)
        tail, tail_i, tail_j, tail_k = torch.chunk(tail, 4, dim=2)

        tail_i, tail_j, tail_k = -tail_i, -tail_j, -tail_k

        tran, tran_i, tran_j, tran_k = torch.chunk(translation, 4, dim=2)  # translations
        rot_h, rot_hi, rot_hj, rot_hk = self.normalize_quaternion(rotator_head)
        rot_t, rot_ti, rot_tj, rot_tk = self.normalize_quaternion(rotator_tail)

        rotated_head_real = head * rot_h - head_i * rot_hi - head_j * rot_hj - head_k * rot_hk
        rotated_head_i = head * rot_hi + head_i * rot_h + head_j * rot_hk - head_k * rot_hj
        rotated_head_j = head * rot_hj - head_i * rot_hk + head_j * rot_h + head_k * rot_hi
        rotated_head_k = head * rot_hk + head_i * rot_hj - head_j * rot_hi + head_k * rot_h

        rotated_tail_real = tail * rot_t - tail_i * rot_ti - tail_j * rot_tj - tail_k * rot_tk
        rotated_tail_i = tail * rot_ti + tail_i * rot_t + tail_j * rot_tk - tail_k * rot_tj
        rotated_tail_j = tail * rot_tj - tail_i * rot_hk + tail_j * rot_t + tail_k * rot_ti
        rotated_tail_k = tail * rot_tk + tail_i * rot_tj - tail_j * rot_ti + tail_k * rot_t

        score_r = rotated_head_real + tran - rotated_tail_real
        score_i = rotated_head_i + tran_i - rotated_tail_i
        score_j = rotated_head_j + tran_j - rotated_tail_j
        score_k = rotated_head_k + tran_k - rotated_tail_k

        score = torch.stack([score_r, score_i, score_j, score_k], dim=0)
        score = score.norm(dim=0)
        return score.sum(dim=2)

    def TransQuatEReg(self, head, rotator_head, mapping_regularizer, translation, tail):
        head_real, head_i, head_j, head_k = torch.chunk(head, 4, dim=2)
        # print("head_real", head_real.shape)
        rot_h, rot_hi, rot_hj, rot_hk = self.normalize_quaternion(rotator_head)  # relationdan gelen bir rotation degree
        # print("rot_h", rot_h.shape)

        # rotating head in 4 dim --- checked
        rotated_head_real = head_real * rot_h - head_i * rot_hi - head_j * rot_hj - head_k * rot_hk
        # print("rotated_head_real", rotated_head_real.shape)
        # print("head_real * rot_h", (head_real * rot_h).shape)
        rotated_head_i = head_real * rot_hi + head_i * rot_h + head_j * rot_hk - head_k * rot_hj
        rotated_head_j = head_real * rot_hj - head_i * rot_hk + head_j * rot_h + head_k * rot_hi
        rotated_head_k = head_real * rot_hk + head_i * rot_hj - head_j * rot_hi + head_k * rot_h

        # now translating head
        tran_real, tran_i, tran_j, tran_k = torch.chunk(translation, 4, dim=2)  # by relation
        # print("tran_real", tran_real.shape)
        translated_head_real = rotated_head_real + tran_real
        # print("translated_head_real = rotated_head_real + tran_real", translated_head_real.shape)
        translated_head_i = rotated_head_i + tran_i
        translated_head_j = rotated_head_j + tran_j
        translated_head_k = rotated_head_k + tran_k
        # -----------------------------------------------------#
        # mapping_regularizer_real, mapping_regularizer_i, mapping_regularizer_j, mapping_regularizer_k = \
        #    torch.chunk(mapping_regularizer, 4, dim=2)
        # TODO:
        mapping_regularizer_real, mapping_regularizer_i, mapping_regularizer_j, mapping_regularizer_k = self.normalize_quaternion(
            mapping_regularizer)
        # print("mapping_regularizer_real", mapping_regularizer_real.shape)

        tail_real, tail_i, tail_j, tail_k = torch.chunk(tail, 4, dim=2)

        # rotating mapping_regularizer in 4 dim by tail
        # --- checked
        rotated_mapping_regularizer_real = mapping_regularizer_real * tail_real - mapping_regularizer_i * tail_i - mapping_regularizer_j * tail_j - mapping_regularizer_k * tail_k
        # print("mapping_regularizer_real * tail_real", (mapping_regularizer_real * tail_real).shape)
        rotated_mapping_regularizer_i = mapping_regularizer_real * tail_i + mapping_regularizer_i * tail_real + mapping_regularizer_j * tail_k - mapping_regularizer_k * tail_j
        rotated_mapping_regularizer_j = mapping_regularizer_real * tail_j - mapping_regularizer_i * tail_k + mapping_regularizer_j * tail_real + mapping_regularizer_k * tail_i
        rotated_mapping_regularizer_k = mapping_regularizer_real * tail_k + mapping_regularizer_i * tail_j - mapping_regularizer_j * tail_i + mapping_regularizer_k * tail_real

        score_r = translated_head_real - rotated_mapping_regularizer_real
        # print("score_r", score_r.shape)
        score_i = translated_head_i - rotated_mapping_regularizer_i
        score_j = translated_head_j - rotated_mapping_regularizer_j
        score_k = translated_head_k - rotated_mapping_regularizer_k

        score = torch.stack([score_r, score_i, score_j, score_k], dim=0)
        # print("score stack", score.shape)
        score = score.norm(dim=0)

        return score.sum(dim=2)

    def QuatEVersor(self, head, rotator_head, rotator_tail, translation, tail):

        s_head, x_head, y_head, z_head = torch.chunk(head, 4, dim=2)
        s_head = torch.tensor([0], dtype=torch.float32).cuda() * s_head

        s_tail, x_tail, y_tail, z_tail = torch.chunk(tail, 4, dim=2)
        s_tail = torch.tensor([0], dtype=torch.float32).cuda() * s_tail

        relation_s, relation_x, relation_y, relation_z = torch.chunk(translation, 4, dim=2)
        l_relation_s, l_relation_x, l_relation_y, l_relation_z = torch.chunk(rotator_head, 4, dim=2)
        r_relation_s, r_relation_x, r_relation_y, r_relation_z = torch.chunk(rotator_tail, 4, dim=2)

        l_relation_x_c = - l_relation_x
        l_relation_y_c = - l_relation_y
        l_relation_z_c = - l_relation_z
        r_relation_x_c = - r_relation_x
        r_relation_y_c = - r_relation_y
        r_relation_z_c = - r_relation_z

        d = torch.stack([l_relation_s, l_relation_x, l_relation_y, l_relation_z], dim=2)
        d1 = d.norm(dim=3)

        denominator_l = torch.sqrt(l_relation_s ** 2 + l_relation_x ** 2 + l_relation_y ** 2 + l_relation_z ** 2)
        denominator_r = torch.sqrt(r_relation_s ** 2 + r_relation_x ** 2 + r_relation_y ** 2 + r_relation_z ** 2)
        denominator_h = torch.sqrt(s_head ** 2 + x_head ** 2 + y_head ** 2 + z_head ** 2)
        denominator_t = torch.sqrt(s_tail ** 2 + x_tail ** 2 + y_tail ** 2 + z_tail ** 2)

        s_head = s_head / (denominator_l * denominator_l)
        x_head = x_head / (denominator_l * denominator_l)
        y_head = y_head / (denominator_l * denominator_l)
        z_head = z_head / (denominator_l * denominator_l)
        s_tail = s_tail / (denominator_r * denominator_r)
        x_tail = x_tail / (denominator_r * denominator_r)
        y_tail = y_tail / (denominator_r * denominator_r)
        z_tail = z_tail / (denominator_r * denominator_r)

        ts_c = s_tail * r_relation_s - x_tail * r_relation_x_c - y_tail * r_relation_y_c - z_tail * r_relation_z_c
        tx_c = s_tail * r_relation_x_c + r_relation_s * x_tail + y_tail * r_relation_z_c - r_relation_y_c * z_tail
        ty_c = s_tail * r_relation_y_c + r_relation_s * y_tail + z_tail * r_relation_x_c - r_relation_z_c * x_tail
        tz_c = s_tail * r_relation_z_c + r_relation_s * z_tail + x_tail * r_relation_y_c - r_relation_x_c * y_tail

        ts = ts_c * r_relation_s - tx_c * r_relation_x - ty_c * r_relation_y - tz_c * r_relation_z
        tx = tx_c * r_relation_s + r_relation_x * ts_c + tz_c * r_relation_y - r_relation_z * ty_c
        ty = ty_c * r_relation_s + r_relation_y * ts_c + tx_c * r_relation_z - r_relation_x * tz_c
        tz = tz_c * r_relation_s + r_relation_z * ts_c + ty_c * r_relation_x - r_relation_y * tx_c

        hs_c = s_head * l_relation_s - x_head * l_relation_x_c - y_head * l_relation_y_c - z_head * l_relation_z_c
        hx_c = s_head * l_relation_x_c + l_relation_s * x_head + y_head * l_relation_z_c - l_relation_y_c * z_head
        hy_c = s_head * l_relation_y_c + l_relation_s * y_head + z_head * l_relation_x_c - l_relation_z_c * x_head
        hz_c = s_head * l_relation_z_c + l_relation_s * z_head + x_head * l_relation_y_c - l_relation_x_c * y_head

        hs = hs_c * l_relation_s - hx_c * l_relation_x - hy_c * l_relation_y - hz_c * l_relation_z
        hx = hx_c * l_relation_s + l_relation_x * hs_c + hz_c * l_relation_y - l_relation_z * hy_c
        hy = hy_c * l_relation_s + l_relation_y * hs_c + hx_c * l_relation_z - l_relation_x * hz_c
        hz = hz_c * l_relation_s + l_relation_z * hs_c + hy_c * l_relation_x - l_relation_y * hx_c

        s = hs + relation_s - ts
        x = hx + relation_x - tx
        y = hy + relation_y - ty
        z = hz + relation_z - tz

        score = torch.stack([s, x, y, z], dim=0)
        score = score.norm(dim=0)
        score = self.gamma.item() - score.sum(dim=2)

        return score

    def l2_regularizer2(self):
        regul = self.entity_embedding.norm() + self.relation_embedding.norm()
        regul = regul + self.rotator_head.norm()
        return regul

    def l2_regularizer(self):
        ent_re, ent_i, ent_j, ent_k = torch.chunk(self.entity_embedding, 4, dim=1)
        tr_re, tr_i, tr_j, tr_k = torch.chunk(self.relation_embedding, 4, dim=1)
        rot_re, rot_i, rot_j, rot_k = torch.chunk(self.rotator_head, 4, dim=1)
        term1 = torch.mean(ent_re ** 2) + torch.mean(ent_i ** 2) + torch.mean(ent_j ** 2) + torch.mean(ent_k ** 2)
        temp2 = torch.mean(tr_re ** 2) + torch.mean(tr_j ** 2) + torch.mean(tr_k ** 2) + torch.mean(tr_i ** 2)
        term3 = torch.mean(rot_re ** 2) + torch.mean(rot_i ** 2) + torch.mean(rot_j ** 2) + torch.mean(rot_k ** 2)
        l2_reg = term1 + temp2 + term3
        '''
        l2_reg = self.entity_embedding.norm()
        print('l2 reg at start - ', l2_reg.item())
        for name, data in self.named_parameters():
            if name in ['relation_embedding', 'rotator_head', 'rotator_tail']:
                #l2_reg = l2_reg + data.norm()
                print('data norm is ', data.norm().item())
                print('updated l2 reg - ', l2_reg.item())
                tmp = torch.sum(data**2)
                print('summed up squared data is ', tmp.item())
        print('final L2 reg term is ', l2_reg.item())
        '''
        return l2_reg

    # ---------------------------------------------------------------------------
    def bce_logits_loss(self, positive_score, negative_score):
        criterion = nn.BCEWithLogitsLoss(reduction='sum')
        positive_score = positive_score.squeeze()
        negative_score = negative_score.view(-1)
        total = positive_score.numel() + negative_score.numel()
        positive_loss = criterion(positive_score, torch.ones(positive_score.shape).cuda())
        negative_loss = criterion(negative_score, torch.zeros(negative_score.shape).cuda())
        loss = (positive_loss + negative_loss) / total
        return positive_loss / positive_score.numel(), negative_loss / negative_score.numel(), loss

    def ruge_loss(self, positive_score, negative_score, unlabeled_scores, soft_labels):
        # TODO: add adversarial temperature??
        positive_loss = self.criterion(positive_score, torch.ones(positive_score.shape).cuda())
        negative_loss = self.criterion(negative_score, torch.zeros(negative_score.shape).cuda())
        total_labeled = positive_score.numel() + negative_score.numel()

        unlabeled_loss = self.criterion(unlabeled_scores.squeeze(), soft_labels.detach())

        l2 = self.l2_regularizer()
        l2 = self.entity_embedding.norm() + self.relation_embedding.norm()

        loss = labeled_loss / total_labeled + unlabeled_loss.mean() + 0.0002 * l2

        loss = (positive_loss + negative_loss) / total_labeled + unlabeled_loss / unlabeled_scores.numel()
        return positive_loss / positive_score.numel(), negative_loss / negative_score.numel(), unlabeled_loss / unlabeled_scores.numel(), loss

    def quate_loss(self, positive_score, negative_score, subsampling_weight, args):
        negative_score = negative_score.view(1, -1).squeeze()
        positive_score = positive_score.squeeze()
        # print('positive score - ', positive_score)
        # print('negative score - ', negative_score)

        # regul1 = torch.mean(h**2) + torch.mean(hi**2) + torch.mean(hj**2) + torch.mean(hk**2) + \
        #         torch.mean(t**2) + torch.mean(ti**2) + torch.mean(tj**2) + torch.mean(tk**2)

        # regul2 = torch.mean(r**2) + torch.mean(ri**2) + torch.mean(rj**2) + torch.mean(rk**2)


        negative_loss = torch.mean(self.criterion(-negative_score))
        positive_loss = torch.mean(self.criterion(positive_score))

        regul1 = self.entity_embedding.norm(p=1) ** 2
        loss = positive_loss + negative_loss

        return positive_loss, negative_loss, loss

    def rotate_loss(self, positive_score, negative_score, subsampling_weight, args):
        #if self.model_name != 'ComplEx' and self.model_name != 'QuatE':
        if self.model_name != 'ComplEx' and self.model_name != 'QuatE':
            negative_score = self.gamma.item() - negative_score
            positive_score = self.gamma.item() - positive_score
        #TODO: quate ise regularizerla topla burda test yap sonra
        if args.negative_adversarial_sampling:
            # In self-adversarial sampling, we do not apply back-propagation on the sampling weight
            negative_score = (F.softmax(negative_score * args.adversarial_temperature, dim=1).detach()
                              * F.logsigmoid(-negative_score)).sum(dim=1)
        else:
            negative_score = F.logsigmoid(-negative_score).mean(dim=1)

        positive_score = F.logsigmoid(positive_score).squeeze(dim=1)

        if args.uni_weight:
            positive_sample_loss = - positive_score.mean()
            negative_sample_loss = - negative_score.mean()
        else:
            positive_sample_loss = - (subsampling_weight * positive_score).sum() / subsampling_weight.sum()
            negative_sample_loss = - (subsampling_weight * negative_score).sum() / subsampling_weight.sum()

        loss = (positive_sample_loss + negative_sample_loss) / 2

        if self.model_name == 'QuatE':
            # Use L3 regularization for ComplEx and DistMult
            regularization = 0.000005 * (
                    self.entity_embedding.norm(p=2) ** 2 +
                    self.relation_embedding.norm(p=2).norm(p=2) ** 2
            )
            loss = loss + regularization
            regularization_log = {'regularization': regularization.item()}

        return positive_sample_loss, negative_sample_loss, loss

    def custom_loss(self, positive_score, negative_score, subsampling_weight, args):
        negative_score = self.gamma2 - negative_score  # model.gamma - negative_score

        if args.negative_adversarial_sampling:
            # In self-adversarial sampling, we do not apply back-propagation on the sampling weight
            negative_score = (F.softmax(negative_score * args.adversarial_temperature, dim=1).detach()
                              * F.relu(negative_score)).sum(dim=1)
        else:
            negative_score = F.relu(negative_score).mean(dim=1)

        positive_score = positive_score - self.gamma1
        positive_score = F.relu(positive_score).squeeze(dim=1)

        if args.uni_weight:
            positive_sample_loss = positive_score.mean()
            negative_sample_loss = negative_score.mean()
        else:
            positive_sample_loss = (subsampling_weight * positive_score).sum() / subsampling_weight.sum()
            negative_sample_loss = (subsampling_weight * negative_score).sum() / subsampling_weight.sum()

        loss = (positive_sample_loss + negative_sample_loss) / 2
        return positive_sample_loss, negative_sample_loss, loss

    def uncertain_loss(self, positive_score, negative_score, subsampling_weight, args):

        if self.model_name != 'ComplEx':
            negative_score = self.gamma.item() - negative_score
            positive_score = positive_score - self.gamma.item()
        else:
            positive_score = -positive_score

        xi = self.xi[self.idx].squeeze(2)
        xi_neg = self.xi_neg[self.idx].squeeze(2)
        xi1 = xi_neg.repeat(1, negative_score.size()[1])

        if args.negative_adversarial_sampling:
            # In self-adversarial sampling, we do not apply back-propagation on the sampling weight
            negative_score = (F.softmax(negative_score * args.adversarial_temperature, dim=1).detach()
                              * F.softplus(negative_score + xi1 ** 2)).sum(dim=1)
        else:
            negative_score = F.softplus(negative_score + xi1 ** 2).mean(dim=1)

        positive_score = F.softplus(positive_score + xi ** 2).squeeze(dim=1)

        temp_pos = torch.exp(-self.alpha * xi ** 2)
        temp_neg = torch.exp(-self.alpha * xi1 ** 2)

        exp = torch.tensor([1000.05]).cuda().float() * torch.sum(temp_pos) \
              + torch.tensor([10.05]).cuda().float() * torch.sum(temp_neg)

        if args.uni_weight:
            positive_sample_loss = positive_score.mean()
            negative_sample_loss = negative_score.mean()
        else:
            positive_sample_loss = (subsampling_weight * positive_score).sum() / subsampling_weight.sum()
            negative_sample_loss = (subsampling_weight * negative_score).sum() / subsampling_weight.sum()

        loss = exp + (positive_sample_loss + negative_sample_loss) / 2
        # print(self.half_margin)

        return positive_sample_loss, negative_sample_loss, loss

    def Limit_Loss(self, positive_score, negative_score, subsampling_weight, args):
        temploss = torch.relu(positive_score + self.gamma - negative_score)  # + self.lda*(positive_score-self.gamma2))
        adv = 1 - F.softmax(negative_score * args.adversarial_temperature, dim=1).detach()
        if args.negative_adversarial_sampling:
            # In self-adversarial sampling, we do not apply back-propagation on the sampling weight
            temploss = (adv * temploss).sum(dim=1)
        else:
            temploss = temploss.mean(dim=1)

        if args.uni_weight:
            positive_sample_loss = positive_score.mean()
            negative_sample_loss = negative_score.mean()
        else:
            positive_sample_loss = positive_score.mean()
            negative_sample_loss = negative_score.mean()

        # yy = torch.tensor([1.0], dtype=torch.float).cuda()
        # mrg = self.gamma.type(torch.cuda.DoubleTensor)
        # criterion = nn.MarginRankingLoss(mrg).cuda()
        # loss = criterion(positive_score, negative_score, yy).cuda()
        loss = torch.mean(temploss)
        # print(loss)
        # y = np.repeat([-1], repeats=positive_score.shape[0])
        # y = torch.tensor(y, dtype=torch.float).cuda()
        # criterion = nn.MarginRankingLoss(
        #     margin=10.0,
        # )
        # loss = nn.MarginRankingLoss(margin=self.gamma.item())(positive_score, negative_score, yy)
        # print(self.gamma.item())
        # exit()
        return positive_sample_loss, negative_sample_loss, loss

    def adaptive_margin_loss(self, positive_score, negative_score, subsampling_weight, args):
        if self.model_name != 'ComplEx':
            negative_score = self.gamma.item() - negative_score
            positive_score = positive_score - self.gamma.item()
        else:
            positive_score = -positive_score
        if args.negative_adversarial_sampling:
            # In self-adversarial sampling, we do not apply back-propagation on the sampling weight
            negative_score = (F.softmax(negative_score * args.adversarial_temperature, dim=1).detach()
                              * F.relu(negative_score + self.margin ** 2)).sum(dim=1)
        else:
            negative_score = F.relu(negative_score + self.margin ** 2).mean(dim=1)

        positive_score = F.relu(positive_score + self.margin ** 2).squeeze(dim=1)

        if args.uni_weight:
            positive_sample_loss = positive_score.mean()
            negative_sample_loss = negative_score.mean()
        else:
            positive_sample_loss = (subsampling_weight * positive_score).sum() / subsampling_weight.sum()
            negative_sample_loss = (subsampling_weight * negative_score).sum() / subsampling_weight.sum()

        loss = self.lambda1 * torch.exp(-self.sigma * self.margin ** 2) + (
                positive_sample_loss + negative_sample_loss) / 2
        return positive_sample_loss, negative_sample_loss, loss

    # ------------------------------------------
    def predict_soft_labels(self, rules, use_cuda):
        longTensor = torch.cuda.LongTensor if use_cuda else torch.LongTensor
        floatTensor = torch.cuda.FloatTensor if use_cuda else torch.FloatTensor

        labeled = [rule.premise for rule in rules]
        unlabeled = [rule.conclusion for rule in rules]
        conf = [rule.conf for rule in rules]
        labeled = longTensor(labeled)
        unlabeled = longTensor(unlabeled)
        conf = floatTensor(conf)

        labeled_scores = self.forward(labeled)
        labeled_scores = labeled_scores.detach()
        labeled_scores = torch.sigmoid(labeled_scores).squeeze()
        unlabeled_scores = self.forward(unlabeled)
        unlabeled_scores_sigmoid = torch.sigmoid(unlabeled_scores).squeeze()

        soft_labels = unlabeled_scores_sigmoid + self.ruge_rule_penalty * conf * labeled_scores
        soft_labels = torch.clamp(soft_labels, min=0, max=1)
        return unlabeled_scores, soft_labels

    # ------------------ LOSS FUNCTIONS FOR GROUNDINGS ----------------------------
    @staticmethod
    def inverse_loss(model, groundings, longTensor):
        sample_premise = torch.index_select(groundings, 1, longTensor([0, 2, 1]))
        sample_conclusion = torch.index_select(groundings, 1, longTensor([1, 3, 0]))
        scores_premise = model(model.idx, sample_premise)
        scores_concl = model(model.idx, sample_conclusion)
        # difference = scores_premise - scores_concl
        # return F.relu(difference + model.epsilon_inv)
        return F.relu(scores_concl - scores_premise + model.epsilon_inv)  # new version
        # return F.relu(scores_premise - scores_concl + model.epsilon_inv)    # old version (reverse)

    @staticmethod
    def implication_loss(model, groundings, longTensor):
        sample_premise = torch.index_select(groundings, 1, longTensor([0, 2, 1]))
        sample_conclusion = torch.index_select(groundings, 1, longTensor([0, 3, 1]))
        scores_premise = model(sample_premise)
        scores_conclusion = model(sample_conclusion)
        # using adaptive margin loss as score
        score = scores_conclusion - scores_premise + model.epsilon_impl
        score = F.relu(score).mean(dim=1)
        return score

        # return F.relu(scores_conclusion - scores_premise + model.epsilon_impl)
        # score = scores_premise - scores_conclusion + model.epsilon_impl
        # return F.relu(score)

    @staticmethod
    def equality_loss(model, groundings, longTensor):
        sample_premise = torch.index_select(groundings, 1, longTensor([0, 2, 1]))
        sample_conclusion = torch.index_select(groundings, 1, longTensor([0, 3, 1]))
        scores_premise = model(sample_premise)
        scores_conclusion = model(sample_conclusion)
        loss = scores_premise - scores_conclusion - model.epsilon_eq
        # return torch.norm(difference - model.epsilon_eq, p = 1)
        # loss = torch.norm(difference, p = 1) - model.epsilon_eq
        # return F.relu(loss)
        # loss = scores_premise - scores_conclusion - model.epsilon_eq
        return torch.norm(loss, p=1)

    @staticmethod
    def symmetry_loss(model, groundings, longTensor):
        sample_premise = torch.index_select(groundings, 1, longTensor([0, 2, 1]))
        sample_conclusion = torch.index_select(groundings, 1, longTensor([1, 2, 0]))
        scores_premise = model(model.idx, sample_premise)
        scores_conclusion = model(model.idx, sample_conclusion)
        loss = scores_premise - scores_conclusion - model.epsilon_sym
        # loss = torch.norm(difference, p = 1) - model.epsilon_sym
        # return F.relu(loss)
        # loss = scores_premise - scores_conclusion - model.epsilon_sym
        return torch.norm(loss, p=1)

    @staticmethod
    def ruge_unlabeled_loss(model, groundings, use_cuda):
        unlabeled_scores, soft_labels = model.predict_soft_labels(groundings, use_cuda)
        unlabeled_scores = unlabeled_scores.squeeze()
        soft_labels = soft_labels.detach()
        softs_notones = soft_labels != 1
        softs_notzeros = soft_labels != 0
        softs = torch.sum(softs_notones == softs_notzeros).item()
        if softs != 0:
            print("FINALLY FOUND {} SOFT LABELS".format(softs))
        criterion = nn.BCEWithLogitsLoss()
        unlabeled_loss = criterion(unlabeled_scores, soft_labels.detach())
        return unlabeled_loss

    @staticmethod
    def adversarial_loss(adv_model, kge_model):
        # compute inconsistnecy loss
        conclusion, premise, conj = adv_model(kge_model, detach=False)  # clause scores
        loss = adv_model.adversarial_loss(conclusion, premise, conj, reverse=True)

        return loss

    # ------------------------------------------------------------------------------
    @staticmethod
    def check_nans(model):
        # print("in check nan")
        nan1 = nan2 = nan3 = nan4 = 0
        nan1 = torch.isnan(model.entity_embedding.grad).sum()
        nan2 = torch.isnan(model.relation_embedding.grad).sum()
        if model.model_name in ['TransRotatE', 'TransQuatE']:
            nan3 = torch.isnan(model.rotator_head.grad).sum()
            nan4 = torch.isnan(model.rotator_tail.grad).sum()
        if model.model_name in ['sTransRotatE', 'sTransQuatE']:
            nan3 = torch.isnan(model.rotator_head.grad).sum()
        if model.model_name in ['TransQuatEReg']:
            nan3 = torch.isnan(model.rotator_head.grad).sum()
            nan4 = torch.isnan(model.mapping_regularizer.grad).sum()

        if nan1 != 0: print(nan1.item(), ' nan vals in entity grads')
        if nan2 != 0: print(nan2.item(), ' nan vals in relation  grad')
        if nan3 != 0: print(nan3.item(), ' nan vals in rotator')
        if nan4 != 0: print(nan4.item(), ' nan vals in rotator/mapping reg')
        if nan1 + nan2 + nan3 + nan4 != 0: exit()

    @staticmethod
    def rule_train_step(model, groundings, use_cuda):
        loss_groundings = {
            'inverse': model.inverse_loss,
            'implication': model.implication_loss,
            'equality': model.equality_loss,
            'symmetry': model.symmetry_loss
        }

        loss_rules = 0
        longTensor = torch.cuda.LongTensor if use_cuda else torch.LongTensor
        rules_log = {}
        for rule_type, grounds in groundings.items():
            if use_cuda:
                grounds = grounds.cuda()
            curr_rule_loss = loss_groundings[rule_type](model, grounds, longTensor)
            curr_rule_loss = curr_rule_loss.mean()
            loss_rules += model.rule_weight[rule_type] * curr_rule_loss
            rules_log[rule_type + ' loss'] = curr_rule_loss.item()

        return loss_rules, rules_log

    @staticmethod
    def ruge_train_step(model, optimizer, train_iterator, args, rules, show_soft):
        ''' A single ruge train step '''

        longTensor = torch.cuda.LongTensor if args.cuda else torch.LongTensor
        floatTensor = torch.cuda.FloatTensor if args.cuda else torch.FloatTensor

        model.train()
        optimizer.zero_grad()

        # predict soft labels
        try:
            rules = rules['ruge']
        except:
            ValueError("No RUGE rules were defined")
        unlabeled_scores, soft_labels = model.predict_soft_labels(rules, args.cuda)
        if show_soft:
            print("Soft labels: ")
            print(soft_labels)

        positive_sample, negative_sample, subsampling_weight, mode = next(train_iterator)
        if args.cuda:
            positive_sample = positive_sample.cuda()
            negative_sample = negative_sample.cuda()
            subsampling_weight = subsampling_weight.cuda()

        negative_score = model((positive_sample, negative_sample), mode=mode)
        positive_score = model(positive_sample)

        loss_args = {
            "positive_score": positive_score,
            'negative_score': negative_score,
            'unlabeled_scores': unlabeled_scores,
            'soft_labels': soft_labels}

        positive_loss, negative_loss, unlabeled_loss, loss = model.Loss(**loss_args)

        if args.regularization != 0.0:
            # Use L3 regularization for ComplEx and DistMult
            regularization = args.regularization * (
                    model.entity_embedding.norm(p=3) ** 3 +
                    model.relation_embedding.norm(p=3).norm(p=3) ** 3
            )
            loss = loss + regularization
            regularization_log = {'regularization': regularization.item()}
        else:
            regularization_log = {}

        loss.backward()
        model.check_nans(model)
        optimizer.step()

        log = {
            **regularization_log,
            'positive_sample_loss': positive_loss,
            'negative_sample_loss': negative_loss,
            'unlabeled_sample_loss': unlabeled_loss,
            'loss': loss.item()
        }

        return log

    @staticmethod
    def train_step(model, adv_model, optimizer, train_iterator, args, rules=None):
        '''
        A single train step. Apply back-propation and return the loss
        '''
        # project

        if args.project or args.adversarial:
            with torch.no_grad():
                model.relation_embedding.data = F.normalize(model.relation_embedding, p=2, dim=1)
                model.entity_embedding.data.copy_(torch.clamp(model.entity_embedding, min=0, max=1))

        model.train()

        optimizer.zero_grad()

        adv_loss = torch.cuda.FloatTensor([0]) if args.cuda else torch.FloatTensor([0])
        adv_log = {}
        if args.adversarial:
            adv_loss = model.adversarial_loss(adv_model, model)
            adv_log['inconsistency loss '] = adv_loss.item()

        # compute scores
        idx, positive_sample, negative_sample, subsampling_weight, mode = next(train_iterator)

        if args.cuda:
            positive_sample = positive_sample.cuda()
            negative_sample = negative_sample.cuda()
            subsampling_weight = subsampling_weight.cuda()

        negative_score = model(idx, (positive_sample, negative_sample), mode=mode)
        positive_score = model(idx, positive_sample)

        loss_args = {
            'positive_score': positive_score,
            'negative_score': negative_score,
        }
        if model.loss_name != 'bce':
            loss_args = {
                **loss_args,
                'subsampling_weight': subsampling_weight,
                'args': args
            }
        positive_loss, negative_loss, loss = model.Loss(**loss_args)

        if not model.inject and not args.ruge_inject:  # no rule injection; just take step
            loss.backward()
            model.check_nans(model)
            optimizer.step()
        # 3 options:
        # -- no rules at all
        # -- inject rules using regularizing terms
        # -- inject ruge rules
        unlabeled_log = {}
        rules_log = {}

        if args.ruge_inject:  # RUGE rule injection
            ruges = rules['ruge']
            ruge_loss = model.ruge_unlabeled_loss(model, rules['ruge'], args.cuda)
            unlabeled_log['unlabeled_loss'] = ruge_loss.item()
            loss = loss + ruge_loss
            loss.backward()
            model.check_nans(model)
            optimizer.step()

        elif rules:  # compute other rules
            # optimizer.zero_grad()
            rule_loss, rules_log = model.rule_train_step(model, rules, args.cuda)
            loss = loss + rule_loss
            if model.inject:
                loss.backward()
                model.check_nans(model)
                optimizer.step()

        log = {
            'positive_sample_loss': positive_loss,
            'negative_sample_loss': negative_loss,
            **adv_log,
            **unlabeled_log,
            # 'margin': model.margin.item(),
            **rules_log,
            'loss': loss.item()
        }
        return log

    @staticmethod
    def test_step(model, test_triples, all_true_triples, args):

        model.eval()

        rank_file_name = str(model.model_name) +"_gamma"+ str(int(model.gamma.item()))+"_dim" + str(model.hidden_dim) +"_"+ str(model.training_start_time)
        rank_file_loc = "data/SD2020/ranked_result_" + str(rank_file_name)+".txt"

        test_dataloader_tail = DataLoader(
            TestDataset(
                test_triples,
                all_true_triples,
                args.nentity,
                args.nrelation,
                'tail-batch'
            ),
            batch_size=args.test_batch_size,
            num_workers=max(1, args.cpu_num // 2),
            collate_fn=TestDataset.collate_fn
        )

        #test_dataset_list = [test_dataloader_head, test_dataloader_tail]
        test_dataset_list = [test_dataloader_tail]

        logs = []

        step = 0
        total_steps = sum([len(dataset) for dataset in test_dataset_list])

        all_entities = model.load_entities(args)

        # h r t?

        all_relations = model.load_relations(args)

        with torch.no_grad():
            for test_dataset in test_dataset_list:
                for idx, positive_sample, negative_sample, filter_bias, mode in test_dataset:
                    # negative samples are corrupted heads or tails depends on mode
                    if args.cuda:
                        positive_sample = positive_sample.cuda()
                        #here
                        #print("positive sample(h_r_t)", positive_sample.size()) ([16, 3])
                        negative_sample = negative_sample.cuda()
                        #print("negative sample(tails)", negative_sample.size()) [16, 68907])
                        filter_bias = filter_bias.cuda()

                    batch_size = positive_sample.size(0)

                    score = model(idx, (positive_sample, negative_sample), mode)
                    # all the pos and neg samples score
                    if model.loss_name == 'quate':
                        score = -score
                    #TODO: for quate?
                    elif model.model_name != 'ComplEx' and model.model_name != 'QuatE':
                        score = model.gamma.item() - score
                    score += filter_bias
                    # Explicitly sort all the entities to ensure that there is no test exposure bias
                    argsort = torch.argsort(score, dim=1, descending=True) #returns index after sort

                    if mode == 'head-batch':

                        positive_arg = positive_sample[:, 0] # get the positive head
                        #positive_rel = positive_sample[:, 1]

                    elif mode == 'tail-batch':

                        positive_arg = positive_sample[:, 2] # get the positive tail
                        #positive_rel = positive_sample[:, 1]
                    else:
                        raise ValueError('mode %s not supported' % mode)
                    ranked_triples = ""
                    for i in range(batch_size):
                        # Notice that argsort is not ranking
                        ranking = (argsort[i, :] == positive_arg[i]).nonzero()
                        assert ranking.size(0) == 1
                        # ranking + 1 is the true ranking used in evaluation metrics
                        ranking = 1 + ranking.item()

                        '''
                        if  args.do_test and  str(all_relations.at[int(positive_rel[
                                                        i].item()), "relations"]) == "hasTypes"  and mode == 'tail-batch' :
                            # if positive_rel[i].item() == 3:  # 3= hasTypes ranking <= 10 and and args.do_test
                            ranked_triples += str(ranking) + "\n"
                            entity = all_entities.at[int(positive_sample[:, 0][i].item()), "entities"]
                            ranked_triples += str(entity + "\t" + all_relations.at[
                                int(positive_rel[i].item()), "relations"] + "\tlist[ ]\n")
                            tsize = argsort[i, :].size(0)
                            data = argsort[i, :]
                            rank = argsort[i, :].nonzero()
                            ranked_triples += "list= ["
                            for j in range(tsize):
                                entity_name = all_entities.at[int(data[j].item()), "entities"]
                                if entity_name in ("Education", "Government", "Company", "Facility"):
                                    ranked_triples += str(
                                        str(entity_name) + "---" + str(int(rank[j].item()) + 1)) + "\n"
                            ranked_triples += "]\n"
                        '''
                        logs.append({
                            'MRR': 1.0 / ranking,
                            'MR': float(ranking),
                            'HITS@1': 1.0 if ranking <= 1 else 0.0,
                            'HITS@3': 1.0 if ranking <= 3 else 0.0,
                            'HITS@10': 1.0 if ranking <= 10 else 0.0,
                        })
                    if step % args.test_log_steps == 0:
                        logging.info('Evaluating the model... (%d/%d)' % (step, total_steps))
                    step += 1

                    #if args.do_test:
                        #print("Creating results of total :"+str(ranked_triples.count("\n"))+" lines")
                        #ranked_triple_file = io.open(rank_file_loc, "a+", encoding="utf-8")
                        #ranked_triple_file.write(ranked_triples)
                        #ranked_triple_file.close()
        metrics = {}
        for metric in logs[0].keys():
            metrics[metric] = sum([log[metric] for log in logs]) / len(logs)

        return metrics


    '''
    @staticmethod
    def test_step(model, test_triples, all_true_triples, args):
        
        #Evaluate the model on test or valid datasets
        

        model.eval()

        rank_file_name = str(model.model_name) +"_gamma"+ str(int(model.gamma.item()))+"_dim" + str(model.hidden_dim) +"_"+ str(model.training_start_time)
        rank_file_loc = "data/SD2020/ranked_result_" + str(rank_file_name)+".txt"

        if args.countries:
            # Countries S* datasets are evaluated on AUC-PR
            # Process test data for AUC-PR evaluation
            sample = list()
            y_true = list()
            for head, relation, tail in test_triples:
                for candidate_region in args.regions:
                    y_true.append(1 if candidate_region == tail else 0)
                    sample.append((head, relation, candidate_region))

            sample = torch.LongTensor(sample)
            if args.cuda:
                sample = sample.cuda()

            with torch.no_grad():
                y_score = model(sample).squeeze(1).cpu().numpy()

            y_true = np.array(y_true)

            # average_precision_score is the same as auc_pr
            auc_pr = average_precision_score(y_true, y_score)

            metrics = {'auc_pr': auc_pr}

        else:
            # Otherwise use standard (filtered) MRR, MR, HITS@1, HITS@3, and HITS@10 metrics
            # Prepare dataloader for evaluation


            #TODO: create new test function by removing head-bacthes
            test_dataloader_head = DataLoader(
                TestDataset(
                    test_triples,
                    all_true_triples,
                    args.nentity,
                    args.nrelation,
                    'head-batch'
                ),
                batch_size=args.test_batch_size,
                num_workers=max(1, args.cpu_num // 2),
                collate_fn=TestDataset.collate_fn
            )

            test_dataloader_tail = DataLoader(
                TestDataset(
                    test_triples,
                    all_true_triples,
                    args.nentity,
                    args.nrelation,
                    'tail-batch'
                ),
                batch_size=args.test_batch_size,
                num_workers=max(1, args.cpu_num // 2),
                collate_fn=TestDataset.collate_fn
            )

            test_dataset_list = [test_dataloader_head, test_dataloader_tail]

            logs = []

            step = 0
            total_steps = sum([len(dataset) for dataset in test_dataset_list])

            all_entities = model.load_entities(args)

            # h r t?

            all_relations = model.load_relations(args)

            with torch.no_grad():
                for test_dataset in test_dataset_list:
                    for idx, positive_sample, negative_sample, filter_bias, mode in test_dataset:
                        # negative samples are corrupted heads or tails depends on mode
                        if args.cuda:
                            positive_sample = positive_sample.cuda()
                            negative_sample = negative_sample.cuda()
                            filter_bias = filter_bias.cuda()

                        batch_size = positive_sample.size(0)

                        score = model(idx, (positive_sample, negative_sample), mode)
                        # all the pos and neg samples score
                        if model.loss_name == 'quate':
                            score = -score
                        elif model.model_name != 'ComplEx':
                            score = model.gamma.item() - score
                        score += filter_bias
                        # Explicitly sort all the entities to ensure that there is no test exposure bias
                        argsort = torch.argsort(score, dim=1, descending=True) #returns index after sort

                        if mode == 'head-batch':

                            positive_arg = positive_sample[:, 0]
                            positive_rel = positive_sample[:, 1]

                        elif mode == 'tail-batch':

                            positive_arg = positive_sample[:, 2]
                            positive_rel = positive_sample[:, 1]
                        else:
                            raise ValueError('mode %s not supported' % mode)
                        ranked_triples = ""
                        for i in range(batch_size):
                            # Notice that argsort is not ranking
                            ranking = (argsort[i, :] == positive_arg[i]).nonzero()
                            assert ranking.size(0) == 1
                            # ranking + 1 is the true ranking used in evaluation metrics
                            ranking = 1 + ranking.item()

                            if  args.do_test and  str(all_relations.at[int(positive_rel[
                                                            i].item()), "relations"]) == "hasTypes"  and mode == 'tail-batch' :
                                # if positive_rel[i].item() == 3:  # 3= hasTypes ranking <= 10 and and args.do_test
                                ranked_triples += str(ranking) + "\n"
                                entity = all_entities.at[int(positive_sample[:, 0][i].item()), "entities"]
                                ranked_triples += str(entity + "\t" + all_relations.at[
                                    int(positive_rel[i].item()), "relations"] + "\tlist[ ]\n")
                                tsize = argsort[i, :].size(0)
                                data = argsort[i, :]
                                rank = argsort[i, :].nonzero()
                                ranked_triples += "list= ["
                                for j in range(tsize):
                                    entity_name = all_entities.at[int(data[j].item()), "entities"]
                                    if entity_name in ("Education", "Government", "Company", "Facility"):
                                        ranked_triples += str(
                                            str(entity_name) + "---" + str(int(rank[j].item()) + 1)) + "\n"
                                ranked_triples += "]\n"

                            logs.append({
                                'MRR': 1.0 / ranking,
                                'MR': float(ranking),
                                'HITS@1': 1.0 if ranking <= 1 else 0.0,
                                'HITS@3': 1.0 if ranking <= 3 else 0.0,
                                'HITS@10': 1.0 if ranking <= 10 else 0.0,
                            })
                        if step % args.test_log_steps == 0:
                            logging.info('Evaluating the model... (%d/%d)' % (step, total_steps))
                        step += 1

                        if args.do_test:
                            print("Creating results of total :"+str(ranked_triples.count("\n"))+" lines")
                            ranked_triple_file = io.open(rank_file_loc, "a+", encoding="utf-8")
                            ranked_triple_file.write(ranked_triples)
                            ranked_triple_file.close()
            metrics = {}
            for metric in logs[0].keys():
                metrics[metric] = sum([log[metric] for log in logs]) / len(logs)

        return metrics
    '''
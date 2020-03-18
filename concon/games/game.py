from abc import ABCMeta, abstractmethod

import torch
import torch.nn as nn
import torch.nn.functional as F
import numpy as np

import itertools as it
import more_itertools as m_it
import collections
from datetime import datetime

from ..utils.logging import DummyLogger, Progress
from ..utils.misc import build_optimizer, Unflatten
from ..utils.modules import build_cnn_decoder_from_args, MultiHeadsClassifier

from ..utils.data import Batch

class Game(metaclass=ABCMeta):
    @abstractmethod
    def test_visualize(self, data_iterator, learning_rate):
        """
        Make Bob dream again!
        """
        pass

    @property
    @abstractmethod
    def agents(self):
        """
        List agents involved in the current round of the game
        """
        pass

    @property
    @abstractmethod
    def optim(self):
        """
        Optimizer involved in the current round of the game
        """
        pass

    @abstractmethod
    def evaluate(self):
        """
        Perform evaluation at the end of each epoch
        """
        pass

    @abstractmethod
    def compute_interaction(self, batches, **state_info):
        """
        Computes one round of the game.
        Input:
            batches as required, agents
        Output:
            rewards, successes, avg_msg_length, losses
        """
        pass

    def train(self):
        for agent in self.agents:  # Sets the agents in training mode
            agent.train()

    def eval(self):
        for agent in self.agents:  # Sets the agents in evaluation mode
            agent.eval()

    def start_episode(self):
        """
        Called before starting a new round of the game. Override for setup behavior.
        """
        self.train() # Sets the current agents in training mode

    def start_epoch(self, data_iterator, summary_writer):
        """
        Called before starting a new epoch of the game. Override for setup/pretrain behavior.
        """
        self.train() # Sets the current agents in training mode

    def end_episode(self, **kwargs):
        """
        Called after finishing a round of the game. Override for cleanup behavior.
        """
        self.eval()

    # Trains the model for one epoch of `steps_per_epoch` steps (each step processes a batch)
    def train_epoch(self, data_iterator, epoch=1, steps_per_epoch=1000, autologger=DummyLogger()):
        """
            Model training function
            Input:
                `data_iterator`, an infinite iterator over (batched) data
                `optim`, the optimizer
            Optional arguments:
                `epoch`: epoch number to display in progressbar
                `steps_per_epoch`: number of steps for epoch
                `event_writer`: tensorboard writer to log evolution of values
        """

        self.start_epoch(data_iterator, autologger.summary_writer)
        with autologger:
            start_i = (epoch * steps_per_epoch)
            end_i = (start_i + steps_per_epoch)
            running_avg_success = 0.
            for index in range(start_i, end_i):
                batch = data_iterator.get_batch(keep_category=autologger.log_lang_progress)
                self.start_episode()

                self.optim.zero_grad()

                loss, *external_output  = self.compute_interaction(batch)

                loss.backward() # Backpropagation

                # Gradient clipping and scaling
                if self.grad_clipping > 0:
                    for agent in self.agents:
                        torch.nn.utils.clip_grad_value_(agent.parameters(), self.grad_clipping)
                if self.grad_scaling > 0:
                    for agent in self.agents:
                        torch.nn.utils.clip_grad_norm_(agent.parameters(), self.grad_scaling)

                self.optim.step()

                udpated_state = autologger.update(
                    loss, *external_output,
                    parameters=(p for a in self.agents for p in a.parameters()),
                    batch=batch,
                    index=index,
                )
                self.end_episode(**udpated_state)

    def save(self, path):
        """
        Save model to file `path`
        """
        state = {
            'agents_state_dicts': [agent.state_dict() for agent in self.agents],
            'optims': [self.optim],
        }
        torch.save(state, path)

    @classmethod
    @abstractmethod
    def load(cls, path, args, _old_model=False):
        pass

    # Caution: as this function pretrains all agents, be careful with shared parameters
    # It is likely that this method should be overriden
    def pretrain_CNNs(self, data_iterator, summary_writer, pretrain_CNN_mode='category-wise', freeze_pretrained_CNN=False, learning_rate=0.0001, nb_epochs=5, steps_per_epoch=1000, display_mode='', pretrain_CNNs_on_eval=False, deconvolution_factory=None, shared=False):
        pretrained_models = {}
        for i, agent in enumerate(self.agents):
            agent_name = ("agent %i" % i)
            pretrained_models[agent_name] = self.pretrain_agent_CNN(agent, data_iterator, summary_writer, pretrain_CNN_mode, freeze_pretrained_CNN, learning_rate, nb_epochs, steps_per_epoch, display_mode, pretrain_CNNs_on_eval, deconvolution_factory, agent_name=agent_name)
        return pretrained_models

    def pretrain_agent_CNN(self, agent, data_iterator, summary_writer, pretrain_CNN_mode='category-wise', freeze_pretrained_CNN=False, learning_rate=0.0001, nb_epochs=5, steps_per_epoch=1000, display_mode='', pretrain_CNNs_on_eval=False, deconvolution_factory=None, agent_name="agent"):
        print(("[%s] pretraining %s…" % (datetime.now(), agent_name)), flush=True)

        if pretrain_CNN_mode != 'auto-encoder':
            pretrained_model = self._pretrain_classif(agent, data_iterator, summary_writer, pretrain_CNN_mode, learning_rate, nb_epochs, steps_per_epoch, display_mode, pretrain_CNNs_on_eval, agent_name)
        else:
            pretrained_model = self._pretrain_ae(agent, data_iterator, summary_writer, pretrain_CNN_mode, deconvolution_factory, learning_rate, nb_epochs, steps_per_epoch, display_mode, pretrain_CNNs_on_eval, agent_name)

        if freeze_pretrained_CNN:
            for p in agent.image_encoder.parameters():
                p.requires_grad = False

        return pretrained_model

    # Pretrains the CNN of an agent in category- or feature-wise mode
    def _pretrain_classif(self, agent, data_iterator, summary_writer, pretrain_CNN_mode='category-wise', learning_rate=0.0001, nb_epochs=5, steps_per_epoch=1000, display_mode='', pretrain_CNNs_on_eval=False, agent_name="agent"):
        loss_tag = 'pretrain/loss_%s_%s' % (agent_name, pretrain_CNN_mode)

        concept_sizes = [len(concept) for concept in data_iterator.concepts]
        xcoder = agent.message_decoder if hasattr(agent, 'message_decoder') else agent.message_encoder
        hidden_size = xcoder.symbol_embeddings.weight.size(1)
        device = next(agent.parameters()).device

        if pretrain_CNN_mode == 'feature-wise':
            # Defines one classification head per non-unary concept
            heads = nn.ModuleList([
                nn.Sequential(
                    nn.Linear(50, 3), # I guess the 3 could be replaced with csize
                    nn.LogSoftmax(dim=1)
                ) for csize in concept_sizes if csize > 1
            ]).to(device)
            get_head_targets = (lambda cat: [v for v, csize in zip(cat, concept_sizes) if csize > 1])
        else:
            heads = nn.ModuleList([
                nn.Sequential(
                    nn.Linear(50, data_iterator.nb_categories),
                    nn.LogSoftmax(dim=1))
                ]
            ).to(device)
            get_head_targets = (lambda cat: [data_iterator.category_idx(cat)])

        optimizer = build_optimizer(it.chain(agent.image_encoder.parameters(), heads.parameters()), learning_rate)
        n_heads = len(heads)

        model = MultiHeadsClassifier(agent.image_encoder, optimizer, heads, n_heads, get_head_targets, device)

        total_items = 0
        for epoch in range(nb_epochs):
            pbar = Progress(display_mode, steps_per_epoch, epoch, logged_items={'L', 'acc'})
            epoch_hits, epoch_items = 0., 0. # TODO Do they need to be floats instead of integers?
            with pbar:
                losses = np.zeros(steps_per_epoch) # For each step of the epoch, the average loss per image and head
                for step_i in range(steps_per_epoch):
                    batch = data_iterator.get_batch(keep_category=True, no_evaluation=(not pretrain_CNNs_on_eval), sampling_strategies=[]) # For each instance of the batch, one original and one target image, but no distractor; only the target will be used
                    
                    hits, loss = model.train(batch)
                    
                    for x in hits: epoch_hits += x.sum().item()
                    epoch_items += batch.size
                    total_items += batch.size

                    if(summary_writer is not None): summary_writer.add_scalar(loss_tag, (loss.item() / batch.size), total_items)
                    pbar.update(L=loss.item(), acc=(epoch_hits / (epoch_items * n_heads)))

                    losses[step_i] = (loss.item() / (n_heads * batch.size)) # Clearly not optimal as we don't get the right std-dev (used below to detect problems in the dataset)

                # Here there could be an evaluation phase

        return model

        # Detects problems in the dataset
        # Should be used with '--evaluation_categories -1'
        # We use the information from the last round of training
        loss_mean = np.mean(losses)
        loss_std = np.std(losses)
        print('loss mean: %s; loss std: %s' % (loss_mean, loss_std))

        with torch.no_grad():
            n = len(data_iterator)
            if(n is None): n = 10000

            batch_size = 128
            for batch_i in range(n // batch_size):
                datapoints = [data_iterator.get_datapoint(i) for i in range((batch_size * batch_i), max((batch_size * (batch_i + 1)), n))]
                batch = Batch(size=batch_size, original=[], target=datapoints, base_distractors=[])
                batch_img = batch.target_img(stack=True)

                activation = model(batch_img)
                targets = batch.category(stack=True, f=category_filter).to(device)

                loss = 0.
                for head, target in zip(heads, torch.unbind(targets, dim=1)):
                    pred = head(activation)
                    loss = F.nll_loss(pred, target, reduction='none') + loss

                losses = loss.cpu().numpy()
                losses /= n_heads
                for i, loss in enumerate(losses):
                    if(loss > 1.0):
                    #if((loss - loss_mean) > (3 * loss_std)):
                        print('Ahah! Datapoint idx=%i (category %s) has a high loss of %s!' % (datapoints[i].idx, datapoints[i].category, loss))

    # Pretrains the CNN of an agent in auto-encoder mode
    def _pretrain_ae(self, agent, data_iterator, summary_writer, pretrain_CNN_mode='auto-encoder', deconvolution_factory=None, learning_rate=0.0001, nb_epochs=5, steps_per_epoch=1000, display_mode='', pretrain_CNNs_on_eval=False, agent_name="agent"):
        loss_tag = 'pretrain/loss_%s_%s' % (agent_name, pretrain_CNN_mode)
        device = next(agent.parameters()).device
        model = nn.Sequential(
            agent.image_encoder,
            Unflatten(),
            deconvolution_factory(),
        ).to(device)

        optimizer = build_optimizer(model.parameters(), learning_rate)

        total_items = 0

        for epoch in range(nb_epochs):
            epoch_loss, epoch_items = 0., 0.
            pbar = Progress(display_mode, steps_per_epoch, epoch, logged_items={'L'})
            with pbar:
                for _ in range(steps_per_epoch):
                    self.optim.zero_grad()

                    batch = data_iterator.get_batch(keep_category=True, no_evaluation=(not pretrain_CNNs_on_eval), sampling_strategies=[])
                    batch_img = batch.target_img(stack=True)

                    output = model(batch_img)

                    loss = F.mse_loss(output, batch_img, reduction="sum")

                    epoch_loss += loss.item()
                    epoch_items += batch_img.size(0)
                    total_items += batch_img.size(0)
                    if(summary_writer is not None): summary_writer.add_scalar(loss_tag, (loss.item() / batch_img.size(0)), total_items)
                    pbar.update(L=(epoch_loss / epoch_items))

                    loss.backward()
                    optimizer.step()

                # Here there could an evaluation phase
        return {'model': model}

    def kill(self, agent):
        '''
        To die, to sleep... to sleep, perchance to dream!
        '''
        def weight_init(submodule):
            try:
                submodule.reset_parameters()
            except:
                pass

        agent.apply(weight_init)

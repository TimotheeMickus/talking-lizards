import torch
import torch.nn as nn
import random

from .aliceBob import AliceBob
from ..agents.sender import Sender
from ..agents.receiver import Receiver
from ..agents.senderReceiver import SenderReceiver

class AliceBobPopulation(AliceBob):
    def __init__(self, args):
        nn.Module.__init__(self)
        
        self.base_alphabet_size = args.base_alphabet_size
        self.max_len_msg = args.max_len

        size = args.population

        if(args.shared):
            self._agents = [SenderReceiver.from_args(args) for _ in range(size)]

            self.senders, self.receivers = zip(*[(agent.sender, agent.receiver) for agent in self._agents])
        else:
            self.senders = [Sender.from_args(args) for _ in range(size)]
            self.receivers = [Receiver.from_args(args) for _ in range(size)]

            self._agents = (self.senders + self.receivers)

        # PyTorch cannot find the parameters of objects that are in a list (like `self.senders` or `self.receivers`)
        parameters = []
        for agent in self._agents: parameters += list(agent.parameters())
        self.agent_parameters = nn.ParameterList(parameters)

        self.use_expectation = args.use_expectation
        self.grad_scaling = args.grad_scaling or 0
        self.grad_clipping = args.grad_clipping or 0
        self.beta_sender = args.beta_sender
        self.beta_receiver = args.beta_receiver
        self.penalty = args.penalty
        self.adaptative_penalty = args.adaptative_penalty

    def to(self, *vargs, **kwargs):
        self = super().to(*vargs, **kwargs)

        #for agent in self._agents: agent.to(*args, **kwargs) # Would that be enough? I'm not sure how `.to` works

        self.senders = [sender.to(*vargs, **kwargs) for sender in self.senders]
        self.receivers = [receiver.to(*vargs, **kwargs) for receiver in self.receivers]

        return self

    def forward(self, batch):
        """
        Input:
            `batch` is a Batch (a kind of named tuple); 'original_img' and 'target_img' are tensors of shape [args.batch_size, *IMG_SHAPE] and 'base_distractors' is a tensor of shape [args.batch_size, 2, *IMG_SHAPE]
        Output:
            `sender_outcome`, sender.Outcome
            `receiver_outcome`, receiver.Outcome
        """

        sender = random.choice(self.senders)
        receiver = random.choice(self.receivers)

        return self._forward(batch, sender, receiver)

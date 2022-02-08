from collections import namedtuple

import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.distributions.categorical import Categorical

from .agent import Agent
from ..utils.modules import MessageEncoder, build_cnn_encoder_from_args

# Structure for outcomes
Outcome = namedtuple("Outcome", ["scores"])

# Scores images according to a message
class Receiver(Agent):
    """
    Defines a receiver policy.
    Based on K presented images and a given message, chooses which image the message refers to.
    """
    def __init__(self, image_encoder, message_encoder):
        super(Agent, self).__init__()

        self.image_encoder = image_encoder
        self.message_encoder = message_encoder

    def encode_message(self, message, length):
        if(self.message_encoder.is_gumbel and self.message_encoder.training):
            return self.message_encoder(message, length).transpose(-2, -1)
        else:
            return self.message_encoder(message, length).unsqueeze(-1)

    def forward(self, images, message, length, charlie_gumbel=False):
        return self.aux_forward(images, self.encode_message(message, length), charlie_gumbel=charlie_gumbel)

    def aux_forward(self, images, encoded_message, charlie_gumbel=False):
        """
            Forward propagation.
            Input:
                `images`, of shape [args.batch_size x K x *IMG_SHAPE], where the first of each K image is the target
                `message`, of shape [args.batch_size x (<=MSG_LEN)], message produced by sender
                `length`, of shape [args.batch_size x 1], length of message produced by sender
            Output:
                `Outcome` containing action taken, entropy, log prob, dist and scores.
        """

        # Encodes the images
        # the three last dimensions encode images channel, width and height;
        # the rest correspond to multi-dimensional batching (per batch item, per timestep, target / distractor /charlie prod...)
        original_size = images.size()[:-3]
        encoded_images = self.image_encoder(images.view(-1, *images.size()[-3:]))
        encoded_images = encoded_images.view(*original_size, -1)

        # Scores the targets
        if charlie_gumbel:
            encoded_message = encoded_message.transpose(1, 2).unsqueeze(-2).flatten(end_dim=1)
            encoded_images = encoded_images.transpose(1, 2).transpose(3, 2).flatten(end_dim=1)
            B, I, T = original_size
            #TODO: the bmm is backwards wrt to the non-Charlie Gumbel setup.
            scores = torch.bmm(encoded_message, encoded_images).view(B, T, I)
        else:
            scores = torch.bmm(encoded_images, encoded_message).squeeze(-1)
        outcome = Outcome(scores=scores)
        return outcome

    @classmethod
    def from_args(cls, args, image_encoder=None, symbol_embeddings=None):
        if(image_encoder is None): image_encoder = build_cnn_encoder_from_args(args)
        message_encoder = MessageEncoder.from_args(args, symbol_embeddings=symbol_embeddings)
        return cls(image_encoder, message_encoder)

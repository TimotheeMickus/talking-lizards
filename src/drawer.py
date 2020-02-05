from collections import namedtuple

import torch
import torch.nn as nn

from modules import MessageEncoder, build_cnn_decoder

Outcome = namedtuple("Outcome", ["action"])

class Drawer(nn.Module):
    def __init__(self, message_encoder=None):
        super(Drawer, self).__init__()
        if message_encoder is None: message_encoder = MessageEncoder()
        self.message_encoder = message_encoder
        self.image_decoder = build_cnn_decoder()

    def forward(self, message, length):
        encoded_message = self.message_encoder(message, length)
        image = self.image_decoder(encoded_message[:,:,None,None])
        outcome = Outcome(action=image)
        return outcome

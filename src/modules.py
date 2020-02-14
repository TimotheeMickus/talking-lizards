import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.distributions.categorical import Categorical

# Message -> vector
class MessageEncoder(nn.Module):
    """
    Encodes a message of discrete symbols in a single vector.
    """
    def __init__(self,
        base_alphabet_size,
        embedding_dim,
        output_dim,
        symbol_embeddings):
        super(MessageEncoder, self).__init__()

        self.symbol_embeddings = symbol_embeddings

        self.lstm = nn.LSTM(embedding_dim, output_dim, 1, batch_first=True)

    def forward(self, message, length):
        """
        Forward propagation.
        Input:
            `message`, of shape [args.batch_size x <=MSG_LEN], message produced by sender
            `length`, of shape [args.batch_size x 1], length of message produced by sender
        Output:
            encoded message, of shape [args.batch_size x output_dim]
        """
        # encode
        embeddings = self.symbol_embeddings(message)
        embeddings = self.lstm(embeddings)[0]
        # select last step corresponding to message
        index = torch.arange(message.size(-1)).expand_as(message).to(message.device)
        output = embeddings.masked_select((index == (length-1)).unsqueeze(-1))

        return output.view(embeddings.size(0), embeddings.size(-1))

    @classmethod
    def from_args(cls, args, symbol_embeddings=None):
        if(symbol_embeddings is None): symbol_embeddings = build_embeddings(args.base_alphabet_size, args.hidden_size, use_bos=False)
        return cls(args.base_alphabet_size, args.hidden_size, args.hidden_size, symbol_embeddings=symbol_embeddings)

# Vector -> message
class MessageDecoder(nn.Module):
    def __init__(self,
        base_alphabet_size,
        embedding_dim,
        output_dim,
        max_msg_len,
        symbol_embeddings,
        ):
        super(MessageDecoder, self).__init__()

        self.symbol_embeddings = symbol_embeddings

        self.lstm = nn.LSTM(embedding_dim, output_dim, 1)
        # project encoded img onto cell
        self.cell_proj = nn.Linear(embedding_dim, embedding_dim)
        # project encoded img onto hidden
        self.hidden_proj = nn.Linear(embedding_dim, embedding_dim)
        # project lstm output onto action space
        self.action_space_proj = nn.Linear(embedding_dim, base_alphabet_size + 1)

        self.max_msg_len = max_msg_len
        self.bos_index = base_alphabet_size + 2
        self.eos_index = 0
        self.padding_idx = base_alphabet_size + 1

    def forward(self, encoded):
        # Initialisation
        last_symbol = torch.ones(encoded.size(0)).long().to(encoded.device) * self.bos_index
        cell = self.cell_proj(encoded).unsqueeze(0)
        hidden = self.hidden_proj(encoded).unsqueeze(0)
        state = (cell, hidden)

        # outputs
        message = []
        log_probs = []
        entropy = []

        # Used in the stopping mechanism (when EOS has been produced)
        has_stopped = torch.zeros(encoded.size(0)).bool().to(encoded.device)
        has_stopped.requires_grad = False

        # produces message
        for i in range(self.max_msg_len):
            output, state = self.lstm(self.symbol_embeddings(last_symbol).unsqueeze(0), state)
            output = self.action_space_proj(output).squeeze(0)

            # selects action
            probs =  F.softmax(output, dim=-1)
            dist = Categorical(probs)
            action = dist.sample() if self.training else probs.argmax(dim=-1)

            # ignores prediction for completed messages
            ent = dist.entropy() * (~has_stopped).float()
            log_p = dist.log_prob(action) * (~has_stopped).float()
            log_probs.append(log_p)
            entropy.append(ent)

            action = action.masked_fill(has_stopped, self.padding_idx)
            message.append(action)

            # If all messages are finished
            has_stopped = has_stopped | (action == self.eos_index)
            if has_stopped.all():
                break

            last_symbol = action

        # converts output to tensor
        message = torch.stack(message, dim=1)
        message_len = (message != self.padding_idx).cumsum(dim=1)[:,-1,None]
        log_probs = torch.stack(log_probs, dim=1)

        # average entropy over timesteps, hence ignore padding
        entropy = torch.stack(entropy, dim=1)
        entropy = entropy.sum(dim=1, keepdim=True)
        entropy = entropy / message_len.float()

        outputs = {
            "entropy":entropy,
            "log_probs":log_probs,
            "message":message,
            "message_len":message_len}
        return outputs

    @classmethod
    def from_args(cls, args, symbol_embeddings=None):
        if(symbol_embeddings is None): symbol_embeddings = build_embeddings(args.base_alphabet_size, args.hidden_size, use_bos=True)
        return cls(
            base_alphabet_size=args.base_alphabet_size,
            embedding_dim=args.hidden_size,
            output_dim=args.hidden_size,
            max_msg_len=args.max_len,
            symbol_embeddings=symbol_embeddings,)

# vector -> vector + random noise
class Randomizer(nn.Module):
    def __init__(self, input_dim, random_dim):
        super(Randomizer, self).__init__()
        self.merging_projection = nn.Linear(input_dim + random_dim, input_dim)
        self.random_dim = random_dim
        self.input_dim = input_dim

    def forward(self, input_vector):
        """
        Input:
            `input_vector` of dimension [BATCH x self.input_dim]
        """
        noise = torch.randn(input_vector.size(0), self.random_dim, device=input_vector.device)
        input_with_noise = torch.cat([input_vector, noise], dim=1)
        merged_input = self.merging_projection(input_with_noise)
        return merged_input

    @classmethod
    def from_args(cls, args):
        return cls(input_dim=args.hidden_size, random_dim=args.hidden_size)


def build_cnn(layer_classes=(), input_channels=(), output_channels=(),
    strides=(), kernel_size=None, paddings=None, flatten_last=True,
    sigmoid_after=False,):
    """
    Factory for convolutionnal encoders.
    Input:
        `layer_classes`: a list of classes to stack, taken from `{"conv", "convTranspose", "maxpool", "avgpool"}`
        `input_channels`: a list of expected input channels per layer
        `output_channels`: a list of expected output channels per layer
        `strides`: a list of strides per layer each layer
        `kernel_size`: a valid kernel size used throughout the convolutionnal network encoder, or a list of kernel sizes per layer
        `padding`: an optional list of (output) padding per layer
        `flatten_last`: flatten output instead of performing batch normalization after the last layer.
    Output:
        `cnn`: a convolutionnal network
    Raises:
        `AssertionError` if the provided lists `layer_classes`, `input_channels`, `output_channels`, and `strides` have different lengths
        `ValueError` if a given layer class is not "conv", "maxpool", or "avgpool"
    """

    lens = map(len, (layer_classes, input_channels, output_channels, strides))
    assert len(set(lens)) == 1, "provided parameters have different lengths!"

    if paddings is None:
        paddings = ([0] * len(layer_classes))
    else:
        assert len(layer_classes) == len(paddings), "provided parameters have different lengths!"

    if (type(kernel_size) is int) or (len(kernel_size) == 2):
        kernel_size = ([kernel_size] * len(layer_classes))
    else:
        assert len(layer_classes) == len(kernel_size), "provided parameters have different lengths!"

    if flatten_last:
        norms = ([nn.BatchNorm2d] * (len(layer_classes) - 1)) + [lambda _ : nn.Flatten()]
    else:
        norms = ([nn.BatchNorm2d] * len(layer_classes))

    layers = []

    for s,i,o,n,l,p,k in zip(
        strides,
        input_channels,
        output_channels,
        norms,
        layer_classes,
        paddings,
        kernel_size,):
        if l == "conv":
            core_layer = nn.Sequential(
                nn.Conv2d(
                    in_channels=i,
                    out_channels=o,
                    kernel_size=k,
                    stride=s,
                    padding=p,),
                nn.ReLU())
        elif l == "convTranspose":
            core_layer = nn.Sequential(
                nn.ConvTranspose2d(
                    in_channels=i,
                    out_channels=o,
                    kernel_size=k,
                    stride=s,
                    output_padding=p,),
                nn.ReLU())
        elif l == "maxpool":
            core_layer = nn.MaxPool2d(
                kernel_size=k,
                stride=s,
                padding=p,)
        elif l == "avgpool":
            core_layer = nn.AvgPool2d(
                kernel_size=k,
                stride=s,
                padding=p,)
        else:
            raise ValueError("layer of type %s is not supported.")
        layers.append(
            nn.Sequential(
                core_layer,
                n(o),
        ))
    if sigmoid_after:
        layers.append(nn.Sigmoid())
    cnn = nn.Sequential(*layers)
    return cnn

def build_cnn_encoder_from_args(args):
    """
    Factory for convolutionnal networks
    """
    layer_classes = (["conv"] * args.conv_layers)
    input_channels = ([args.img_channel] + [args.filters] * (args.conv_layers - 1))
    output_channels = ([args.filters] * (args.conv_layers - 1) + [args.hidden_size])
    return build_cnn(
        layer_classes=layer_classes,
        input_channels=input_channels,
        output_channels=output_channels,
        strides=args.strides,
        kernel_size=args.kernel_size,
        paddings=None,)

def build_cnn_decoder_from_args(args):
    """
    Factory for deconvolutionnal networks
    """
    layer_classes = (["convTranspose"] * args.conv_layers)
    strides = args.strides[::-1]
    inputs = [args.hidden_size] + ([args.filters] * (args.conv_layers - 1))
    outputs = ([args.filters] * (args.conv_layers - 1)) + [args.img_channel]
    paddings = [0, 0, 1, 0, 0, 0, 0, 1] # guessworking it out
    return build_cnn(
        layer_classes=layer_classes,
        input_channels=inputs,
        output_channels=outputs,
        strides=strides,
        paddings=paddings,
        kernel_size=args.kernel_size,
        flatten_last=False,
        sigmoid_after=True,)

def build_embeddings(base_alphabet_size, dim, use_bos=False):
    vocab_size = (base_alphabet_size + 3) if use_bos else (base_alphabet_size + 2) # +3: EOS symbol, padding symbol, BOS symbol; +2: EOS symbol, padding symbol
    return nn.Embedding(vocab_size, dim, padding_idx=base_alphabet_size + 1)

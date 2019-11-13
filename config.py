ALPHABET_SIZE = 17 + 1 # + 1 for EOS
EOS, PAD = 0, ALPHABET_SIZE
MSG_LEN = 7
NUMBER_OF_DISTRACTORS = 2
K = NUMBER_OF_DISTRACTORS + 1 # size of pools of image for listener

HIDDEN = 50

CONV_LAYERS = 8
FILTERS = 32
STRIDES = (2, 2, 1, 2, 1, 2, 1, 2) # the original paper suggests 2,1,1,2,1,2,1,2, but that doesn't match the expected output of 50, 1, 1
KERNEL_SIZE = 3

BATCH_SIZE = 32
LR = .0001
BETA_S = .01
BETA_L = .001

#IMG_SHAPE = (3, 124, 124) # Original dataset size
IMG_SHAPE = (3, 124, 124) # COIL size

DATASET_PATH = "/home/tmickus/data/img/coil/coil-100/png/"

DEVICE = "cuda"

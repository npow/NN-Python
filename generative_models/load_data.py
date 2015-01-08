import numpy as np
import numpy.random as npr
import cPickle
import gzip
import os
import sys

import theano
import theano.tensor as T

def _shared_dataset(data_xy):
    """
    Function that loads the dataset into shared variables

    The reason we store our dataset in shared variables is to allow
    Theano to copy it into the GPU memory (when code is run on GPU).
    Since copying data into the GPU is slow, copying a minibatch everytime
    is needed (the default behaviour if the data is not in a shared
    variable) would lead to a large decrease in performance.
    """
    data_x, data_y = data_xy
    shared_x = theano.shared(np.asarray(data_x,
                                           dtype=theano.config.floatX))
    shared_y = theano.shared(np.asarray(data_y,
                                           dtype=theano.config.floatX))
    # When storing data on the GPU it has to be stored as floats
    # therefore we will store the labels as ``floatX`` as well
    # (``shared_y`` does exactly that).
    return shared_x, shared_y

def load_mnist(path, zero_mean=True):
    mnist = np.load(path)
    train_set_x = mnist['train_data']
    train_set_y = mnist['train_labels'] + 1
    test_set_x = mnist['test_data']
    test_set_y = mnist['test_labels'] + 1

    if zero_mean:
        obs_mean = np.mean(train_set_x, axis=0, keepdims=True)
        train_set_x = train_set_x - obs_mean
        test_set_x = test_set_x - obs_mean

    train_set_x, train_set_y = _shared_dataset((train_set_x, train_set_y))
    test_set_x, test_set_y = _shared_dataset((test_set_x, test_set_y))
    valid_set_x, valid_set_y = test_set_x, test_set_y

    rval = [(train_set_x, train_set_y), (valid_set_x, valid_set_y),
            (test_set_x, test_set_y)]
    return rval

def load_udm_ss(dataset, sup_count, rng, zero_mean=True):
    """
    Load semi-supervised version of the standard UdM MNIST data.

    For this, the training data is split into labeled and unlabeled portions.
    The number of labeled examples is 'sup_count', and an equal number of
    labeled examples will be selected for each class. The remaining (50000 -
    sup_count) examples are provided as unlabeled training data. The validate
    and test sets are left unchanged.

    Note: labels for the normal digit classes will range from 1-10, i.e. +1
    compared to their standard value, as 'un-classed' examples take label 0.
    """

    udm_data = load_udm(dataset, as_shared=False, zero_mean=zero_mean)
    Xtr = udm_data[0][0]
    Ytr = udm_data[0][1][:,np.newaxis]

    all_count = Xtr.shape[0]
    pc_count = int(np.ceil(sup_count / 10.0))
    sup_count = int(10 * pc_count)
    unsup_count = all_count - sup_count

    Xtr_su = []
    Ytr_su = []
    Xtr_un = []
    Ytr_un = []

    # Sample supervised and unsupervised subsets of each class' observations
    for c_label in np.unique(Ytr):
        c_idx = [i for i in range(all_count) if (Ytr[i] == c_label)]
        rng.shuffle(c_idx)
        Xtr_su.append(Xtr[c_idx[0:pc_count],:])
        Ytr_su.append(Ytr[c_idx[0:pc_count],:])
        Xtr_un.append(Xtr[c_idx[pc_count:],:])
        Ytr_un.append(Ytr[c_idx[pc_count:],:])

    # Stack per-class supervised/unsupervised splits into matrices
    Xtr_su = np.vstack(Xtr_su)
    Ytr_su = np.vstack(Ytr_su)
    Xtr_un = np.vstack(Xtr_un)
    Ytr_un = np.vstack(Ytr_un)
    # Also keep "unsupervised" copies of the "supervised" data
    Xtr_un = Xtr_un #np.vstack([Xtr_un, Xtr_su])
    Ytr_un = 0 * Ytr_un #np.vstack([Ytr_un, Ytr_su])

    # Shuffle the rows so that observations are not grouped by class
    shuf_idx = rng.permutation(Xtr_su.shape[0])
    Xtr_su = Xtr_su[shuf_idx,:]
    Ytr_su = Ytr_su[shuf_idx].ravel() + 1
    shuf_idx = rng.permutation(Xtr_un.shape[0])
    Xtr_un = Xtr_un[shuf_idx,:]
    Ytr_un = Ytr_un[shuf_idx].ravel()

    # Put matrices into GPU shared variables, for great justice
    Xtr_su, Ytr_su = _shared_dataset((Xtr_su, Ytr_su))
    Xtr_un, Ytr_un = _shared_dataset((Xtr_un, Ytr_un))
    Xva, Yva = _shared_dataset((udm_data[1][0], (udm_data[1][1] + 1)))
    Xte, Yte = _shared_dataset((udm_data[2][0], (udm_data[2][1] + 1)))

    rval = [(Xtr_su, Ytr_su), (Xtr_un, Ytr_un), (Xva, Yva), (Xte, Yte)]

    return rval

def load_udm(dataset, as_shared=True, zero_mean=True):
    """Loads the UdM train/validate/test split of MNIST."""

    #############
    # LOAD DATA #
    #############

    # Download the MNIST dataset if it is not present
    data_dir, data_file = os.path.split(dataset)
    if (not os.path.isfile(dataset)) and data_file == 'mnist.pkl.gz':
        import urllib
        origin = 'http://www.iro.umontreal.ca/~lisa/deep/data/mnist/mnist.pkl.gz'
        print 'Downloading data from %s' % origin
        urllib.urlretrieve(origin, dataset)

    print '... loading data'

    # Load the dataset
    f = gzip.open(dataset, 'rb')
    train_set, valid_set, test_set = cPickle.load(f)
    f.close()
    #train_set, valid_set, test_set format: tuple(input, target)
    #input is an np.ndarray of 2 dimensions (a matrix)
    #witch row's correspond to an example. target is a
    #np.ndarray of 1 dimensions (vector)) that have the same length as
    #the number of rows in the input. It should give the target
    #target to the example with the same index in the input.
    train_set = [v for v in train_set]
    valid_set = [v for v in valid_set]
    test_set = [v for v in test_set]
    train_set[0] = np.asarray(train_set[0]).astype(np.float32)
    valid_set[0] = np.asarray(valid_set[0]).astype(np.float32)
    test_set[0] = np.asarray(test_set[0]).astype(np.float32)
    if zero_mean:
        obs_mean = np.mean(train_set[0], axis=0, keepdims=True)
        train_set[0] = train_set[0] - obs_mean
        valid_set[0] = valid_set[0] - obs_mean
        test_set[0] = test_set[0] - obs_mean
    if as_shared:
        test_set_x, test_set_y = _shared_dataset((test_set[0],test_set[1]+1))
        valid_set_x, valid_set_y = _shared_dataset((valid_set[0],valid_set[1]+1))
        train_set_x, train_set_y = _shared_dataset((train_set[0],train_set[1]+1))
    else:
        test_set_x, test_set_y = test_set
        valid_set_x, valid_set_y = valid_set
        train_set_x, train_set_y = train_set

    rval = [(train_set_x, train_set_y), (valid_set_x, valid_set_y),
            (test_set_x, test_set_y)]
    return rval

def load_svhn(tr_file, te_file, ex_file=None, ex_count=None):
    """
    Loads the full SVHN train/test sets and an additional number of randomly
    selected examples from the "extra set".
    """
    # load the training set as a numpy arrays
    pickle_file = open(tr_file)
    data_dict = cPickle.load(pickle_file)
    Xtr = data_dict['X'].astype(theano.config.floatX)
    Ytr = data_dict['y'].astype(np.int32) + 1
    Xtr_vec = np.zeros((Xtr.shape[3], 32*32*3)).astype(theano.config.floatX)
    for i in range(Xtr.shape[3]):
        c_pix = 32*32
        for c in range(3):
            Xtr_vec[i,c*c_pix:((c+1)*c_pix)] = \
                    Xtr[:,:,c,i].reshape((32*32,))
    Xtr = Xtr_vec
    pickle_file.close()
    # load the test set as numpy arrays
    pickle_file = open(te_file)
    data_dict = cPickle.load(pickle_file)
    Xte = data_dict['X'].astype(theano.config.floatX)
    Yte = data_dict['y'].astype(np.int32) + 1
    Xte_vec = np.zeros((Xte.shape[3], 32*32*3)).astype(theano.config.floatX)
    for i in range(Xte.shape[3]):
        c_pix = 32*32
        for c in range(3):
            Xte_vec[i,c*c_pix:((c+1)*c_pix)] = \
                    Xte[:,:,c,i].reshape((32*32,))
    Xte = Xte_vec
    pickle_file.close()
    if ex_file is None:
        Xex = None
    else:
        # load the extra digit examples and only keep a random subset
        pickle_file = open(ex_file)
        data_dict = cPickle.load(pickle_file)
        ex_full_size = data_dict['X'].shape[3]
        idx = npr.randint(low=0, high=ex_full_size, size=(ex_count))
        Xex = data_dict['X'].take(idx, axis=3).astype(theano.config.floatX)
        Xex_vec = np.zeros((Xex.shape[3], 32*32*3)).astype(theano.config.floatX)
        for i in range(Xex.shape[3]):
            c_pix = 32*32
            for c in range(3):
                Xex_vec[i,c*c_pix:((c+1)*c_pix)] = \
                        Xex[:,:,c,i].reshape((32*32,))
        Xex = Xex_vec
        pickle_file.close()

    # package data up for easy returnage
    data_dict = {'Xtr': Xtr, 'Ytr': Ytr, \
                 'Xte': Xte, 'Yte': Yte, \
                 'Xex': Xex}
    return data_dict

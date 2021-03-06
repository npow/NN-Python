import time
import utils as utils
import numpy as np
import numpy.random as npr
import theano
import theano.tensor as T

from load_data import load_tfd
from PeaNet import PeaNet, load_peanet_from_file
from InfNet import InfNet, load_infnet_from_file
from GenNet import GenNet, load_gennet_from_file
from VCGLoop import VCGLoop
from GIPair import GIPair
from NetLayers import relu_actfun, softplus_actfun, \
                      safe_softmax, safe_log
import GenNet as GNet
import InfNet as INet
import PeaNet as PNet
from DKCode import PCA_theano

import sys, resource
resource.setrlimit(resource.RLIMIT_STACK, (2**29,-1))
sys.setrecursionlimit(10**6)

# DERP
#RESULT_PATH = "TFD_WALKOUT_TEST_KLD/"
#RESULT_PATH = "TFD_WALKOUT_TEST_VAE/"
#RESULT_PATH = "TFD_WALKOUT_TEST_100D_LARGE/"
RESULT_PATH = "TFD_WALKOUT_TEST_50D_SMALL/"
PRIOR_DIM = 50

#####################################
# HELPER FUNCTIONS FOR DATA MASKING #
#####################################

def sample_masks(X, drop_prob=0.3):
    """
    Sample a binary mask to apply to the matrix X, with rate mask_prob.
    """
    probs = npr.rand(*X.shape)
    mask = 1.0 * (probs > drop_prob)
    return mask.astype(theano.config.floatX)

def sample_patch_masks(X, im_shape, patch_shape):
    """
    Sample a random patch mask for each image in X.
    """
    obs_count = X.shape[0]
    rs = patch_shape[0]
    cs = patch_shape[1]
    off_row = npr.randint(1,high=(im_shape[0]-rs-1), size=(obs_count,))
    off_col = npr.randint(1,high=(im_shape[1]-cs-1), size=(obs_count,))
    dummy = np.zeros(im_shape)
    mask = np.zeros(X.shape)
    for i in range(obs_count):
        dummy = (0.0 * dummy) + 1.0
        dummy[off_row[i]:(off_row[i]+rs), off_col[i]:(off_col[i]+cs)] = 0.0
        mask[i,:] = dummy.ravel()
    return mask.astype(theano.config.floatX)

def posterior_klds(IN, Xtr, batch_size, batch_count):
    """
    Get posterior KLd cost for some inputs from Xtr.
    """
    post_klds = []
    for i in range(batch_count):
        batch_idx = npr.randint(low=0, high=Xtr.shape[0], size=(batch_size,))
        X = Xtr.take(batch_idx, axis=0)
        post_klds.extend([k for k in IN.kld_func(X)])
    return post_klds


####################################
####################################
## VAE PRETRAINING FOR THE GIPAIR ##
####################################
####################################

def pretrain_gip(extra_lam_kld=0.0, kld2_scale=0.0):
    # Initialize a source of randomness
    rng = np.random.RandomState(1234)

    # Load some data to train/validate/test with
    data_file = 'data/tfd_data_48x48.pkl'
    dataset = load_tfd(tfd_pkl_name=data_file, which_set='unlabeled', fold='all')
    Xtr_unlabeled = dataset[0]
    dataset = load_tfd(tfd_pkl_name=data_file, which_set='train', fold='all')
    Xtr_train = dataset[0]
    Xtr = np.vstack([Xtr_unlabeled, Xtr_train])
    dataset = load_tfd(tfd_pkl_name=data_file, which_set='valid', fold='all')
    Xva = dataset[0]
    tr_samples = Xtr.shape[0]
    va_samples = Xva.shape[0]
    batch_size = 300
    batch_reps = 10

    # setup some symbolic variables and stuff
    Xp = T.matrix('Xp_base')
    Xd = T.matrix('Xd_base')
    Xc = T.matrix('Xc_base')
    Xm = T.matrix('Xm_base')
    data_dim = Xtr.shape[1]
    prior_sigma = 1.0

    ##########################
    # NETWORK CONFIGURATIONS #
    ##########################
    gn_params = {}
    gn_config = [PRIOR_DIM, 1500, 1500, data_dim]
    gn_params['mlp_config'] = gn_config
    gn_params['activation'] = relu_actfun
    gn_params['out_type'] = 'gaussian'
    gn_params['mean_transform'] = 'sigmoid'
    gn_params['logvar_type'] = 'single_shared'
    gn_params['init_scale'] = 1.0
    gn_params['lam_l2a'] = 1e-2
    gn_params['vis_drop'] = 0.0
    gn_params['hid_drop'] = 0.0
    gn_params['bias_noise'] = 0.0
    # choose some parameters for the continuous inferencer
    in_params = {}
    shared_config = [data_dim, 1500, 1500]
    top_config = [shared_config[-1], PRIOR_DIM]
    in_params['shared_config'] = shared_config
    in_params['mu_config'] = top_config
    in_params['sigma_config'] = top_config
    in_params['activation'] = relu_actfun
    in_params['init_scale'] = 1.0
    in_params['lam_l2a'] = 1e-2
    in_params['vis_drop'] = 0.0
    in_params['hid_drop'] = 0.0
    in_params['bias_noise'] = 0.0
    in_params['input_noise'] = 0.0
    in_params['kld2_scale'] = kld2_scale
    # Initialize the base networks for this GIPair
    IN = InfNet(rng=rng, Xd=Xd, prior_sigma=prior_sigma, \
            params=in_params, shared_param_dicts=None)
    GN = GenNet(rng=rng, Xp=Xp, prior_sigma=prior_sigma, \
            params=gn_params, shared_param_dicts=None)
    # Initialize biases in IN and GN
    IN.init_biases(0.2)
    GN.init_biases(0.2)

    ######################################
    # LOAD AND RESTART FROM SAVED PARAMS #
    ######################################
    # gn_fname = RESULT_PATH+"pt_gip_params_b110000_GN.pkl"
    # in_fname = RESULT_PATH+"pt_gip_params_b110000_IN.pkl"
    # IN = INet.load_infnet_from_file(f_name=in_fname, rng=rng, Xd=Xd, \
    #         new_params=None)
    # GN = GNet.load_gennet_from_file(f_name=gn_fname, rng=rng, Xp=Xp, \
    #         new_params=None)
    # in_params = IN.params
    # gn_params = GN.params

    #########################
    # INITIALIZE THE GIPAIR #
    #########################
    GIP = GIPair(rng=rng, Xd=Xd, Xc=Xc, Xm=Xm, g_net=GN, i_net=IN, \
            data_dim=data_dim, prior_dim=PRIOR_DIM, params=None)
    GIP.set_lam_l2w(1e-4)

    ######################
    # BASIC VAE TRAINING #
    ######################
    out_file = open(RESULT_PATH+"pt_gip_results.txt", 'wb')
    # Set initial learning rate and basic SGD hyper parameters
    cost_1 = [0. for i in range(10)]
    learn_rate = 0.001
    for i in range(110001, 500000):
        scale = min(1.0, float(i) / 20000.0)
        if (i > 75000) and ((i + 1) % 50000 == 0):
            learn_rate = learn_rate * 0.5
        # do a minibatch update of the model, and compute some costs
        tr_idx = npr.randint(low=0,high=tr_samples,size=(batch_size,))
        Xd_batch = Xtr.take(tr_idx, axis=0)
        Xd_batch = np.repeat(Xd_batch, batch_reps, axis=0)
        Xc_batch = 0.0 * Xd_batch
        Xm_batch = 0.0 * Xd_batch
        # do a minibatch update of the model, and compute some costs
        GIP.set_all_sgd_params(lr_gn=(scale*learn_rate), \
                lr_in=(scale*learn_rate), mom_1=0.9, mom_2=0.99)
        #GIP.set_lr(lr=(2.0*scale_1*learn_rate), net='IN')
        GIP.set_lam_nll(1.0)
        GIP.set_lam_kld(1.0 + extra_lam_kld*scale)
        outputs = GIP.train_joint(Xd_batch, Xc_batch, Xm_batch)
        cost_1 = [(cost_1[k] + 1.*outputs[k]) for k in range(len(outputs))]
        if ((i % 1000) == 0):
            cost_1 = [(v / 1000.) for v in cost_1]
            o_str = "batch: {0:d}, joint_cost: {1:.4f}, data_nll_cost: {2:.4f}, post_kld_cost: {3:.4f}, other_reg_cost: {4:.4f}".format( \
                    i, cost_1[0], cost_1[1], cost_1[2], cost_1[3])
            print(o_str)
            out_file.write(o_str+"\n")
            out_file.flush()
            cost_1 = [0. for v in cost_1]
        if ((i % 5000) == 0):
            cost_2 = GIP.compute_costs(Xva, 0.*Xva, 0.*Xva)
            o_str = "--val: {0:d}, joint_cost: {1:.4f}, data_nll_cost: {2:.4f}, post_kld_cost: {3:.4f}, other_reg_cost: {4:.4f}".format( \
                    i, 1.*cost_2[0], 1.*cost_2[1], 1.*cost_2[2], 1.*cost_2[3])
            print(o_str)
            out_file.write(o_str+"\n")
            out_file.flush()
        if ((i % 5000) == 0):
            tr_idx = npr.randint(low=0,high=va_samples,size=(100,))
            Xd_batch = Xva.take(tr_idx, axis=0)
            file_name = RESULT_PATH+"pt_gip_chain_samples_b{0:d}.png".format(i)
            Xd_samps = np.repeat(Xd_batch[0:10,:], 3, axis=0)
            sample_lists = GIP.sample_from_chain(Xd_samps, loop_iters=20)
            Xs = np.vstack(sample_lists["data samples"])
            utils.visualize_samples(Xs, file_name, num_rows=20)
            # draw samples freely from the generative model's prior
            file_name = RESULT_PATH+"pt_gip_prior_samples_b{0:d}.png".format(i)
            Xs = GIP.sample_from_prior(20*20)
            utils.visualize_samples(Xs, file_name, num_rows=20)
            # draw inference net first layer weights
            file_name = RESULT_PATH+"pt_gip_inf_weights_b{0:d}.png".format(i)
            utils.visualize_samples(GIP.IN.W_rica.get_value(borrow=False).T, \
                    file_name, num_rows=20)
            # draw generator net final layer weights
            file_name = RESULT_PATH+"pt_gip_gen_weights_b{0:d}.png".format(i)
            utils.visualize_samples(GIP.GN.W_rica.get_value(borrow=False), \
                    file_name, num_rows=20)
            #########################
            # Check posterior KLds. #
            #########################
            post_klds = posterior_klds(IN, Xtr, 5000, 5)
            file_name = RESULT_PATH+"pt_gip_post_klds_b{0:d}.png".format(i)
            utils.plot_kde_histogram2( \
                    np.asarray(post_klds), np.asarray(post_klds), file_name, bins=30)
            #########################################################
            # Compute some information about approximate posteriors #
            #########################################################
            post_stats = GIP.compute_post_stats(Xva, 0.0*Xva, 0.0*Xva)
            all_post_klds = np.sort(post_stats[0].ravel()) # post KLds for each obs and dim
            obs_post_klds = np.sort(post_stats[1]) # summed post KLds for each obs
            post_dim_klds = post_stats[2] # average post KLds for each post dim
            post_dim_vars = post_stats[3] # average squared mean for each post dim
            utils.plot_line(np.arange(all_post_klds.shape[0]), all_post_klds, RESULT_PATH+"PPP_ALL_POST_KLDS_b{0:d}.png".format(i))
            utils.plot_line(np.arange(obs_post_klds.shape[0]), obs_post_klds, RESULT_PATH+"PPP_OBS_POST_KLDS_b{0:d}.png".format(i))
            utils.plot_stem(np.arange(post_dim_klds.shape[0]), post_dim_klds, RESULT_PATH+"PPP_POST_DIM_KLDS_b{0:d}.png".format(i))
            utils.plot_stem(np.arange(post_dim_vars.shape[0]), post_dim_vars, RESULT_PATH+"PPP_POST_DIM_VARS_b{0:d}.png".format(i))
        if ((i % 10000) == 0):
            IN.save_to_file(f_name=RESULT_PATH+"pt_gip_params_b{0:d}_IN.pkl".format(i))
            GN.save_to_file(f_name=RESULT_PATH+"pt_gip_params_b{0:d}_GN.pkl".format(i))
    IN.save_to_file(f_name=RESULT_PATH+"pt_gip_params_IN.pkl")
    GN.save_to_file(f_name=RESULT_PATH+"pt_gip_params_GN.pkl")
    return

#####################################################
# Train a VCGLoop starting from a pretrained GIPair #
#####################################################

def train_walk_from_pretrained_gip(extra_lam_kld=0.0):
    # Simple test code, to check that everything is basically functional.
    print("TESTING...")

    # Initialize a source of randomness
    rng = np.random.RandomState(1234)

    # Load some data to train/validate/test with
    data_file = 'data/tfd_data_48x48.pkl'
    dataset = load_tfd(tfd_pkl_name=data_file, which_set='unlabeled', fold='all')
    Xtr_unlabeled = dataset[0]
    dataset = load_tfd(tfd_pkl_name=data_file, which_set='train', fold='all')
    Xtr_train = dataset[0]
    Xtr = np.vstack([Xtr_unlabeled, Xtr_train])
    dataset = load_tfd(tfd_pkl_name=data_file, which_set='valid', fold='all')
    Xva = dataset[0]
    print("Xtr.shape: {0:s}, Xva.shape: {1:s}".format(str(Xtr.shape),str(Xva.shape)))

    # get and set some basic dataset information
    tr_samples = Xtr.shape[0]
    va_samples = Xva.shape[0]
    data_dim = Xtr.shape[1]
    batch_size = 300
    batch_reps = 5
    prior_sigma = 1.0
    Xtr_mean = np.mean(Xtr, axis=0, keepdims=True)
    Xtr_mean = (0.0 * Xtr_mean) + np.mean(np.mean(Xtr,axis=1))
    Xc_mean = np.repeat(Xtr_mean, batch_size, axis=0)

    # Symbolic inputs
    Xd = T.matrix(name='Xd')
    Xc = T.matrix(name='Xc')
    Xm = T.matrix(name='Xm')
    Xt = T.matrix(name='Xt')
    Xp = T.matrix(name='Xp')

    START_FRESH = True
    if START_FRESH:
        ###############################
        # Setup discriminator network #
        ###############################
        # Set some reasonable mlp parameters
        dn_params = {}
        # Set up some proto-networks
        pc0 = [data_dim, (300, 4), (300, 4), 10]
        dn_params['proto_configs'] = [pc0]
        # Set up some spawn networks
        sc0 = {'proto_key': 0, 'input_noise': 0.1, 'bias_noise': 0.1, 'do_dropout': True}
        #sc1 = {'proto_key': 0, 'input_noise': 0.1, 'bias_noise': 0.1, 'do_dropout': True}
        dn_params['spawn_configs'] = [sc0]
        dn_params['spawn_weights'] = [1.0]
        # Set remaining params
        dn_params['init_scale'] = 0.25
        dn_params['lam_l2a'] = 1e-2
        dn_params['vis_drop'] = 0.2
        dn_params['hid_drop'] = 0.5
        # Initialize a network object to use as the discriminator
        DN = PeaNet(rng=rng, Xd=Xd, params=dn_params)
        DN.init_biases(0.0)

        #######################################################
        # Load inferencer and generator from saved parameters #
        #######################################################
        gn_fname = RESULT_PATH+"pt_gip_params_b150000_GN.pkl"
        in_fname = RESULT_PATH+"pt_gip_params_b150000_IN.pkl"
        IN = INet.load_infnet_from_file(f_name=in_fname, rng=rng, Xd=Xd)
        GN = GNet.load_gennet_from_file(f_name=gn_fname, rng=rng, Xp=Xp)
    else:
        ###########################################################
        # Load all networks from partially-trained VCGLoop params #
        ###########################################################
        gn_fname = RESULT_PATH+"pt_walk_params_GN.pkl"
        in_fname = RESULT_PATH+"pt_walk_params_IN.pkl"
        dn_fname = RESULT_PATH+"pt_walk_params_DN.pkl"
        IN = INet.load_infnet_from_file(f_name=in_fname, rng=rng, Xd=Xd)
        GN = GNet.load_gennet_from_file(f_name=gn_fname, rng=rng, Xp=Xp)
        DN = PNet.load_peanet_from_file(f_name=dn_fname, rng=rng, Xd=Xd)

    ###############################
    # Initialize the main VCGLoop #
    ###############################
    vcgl_params = {}
    vcgl_params['lam_l2d'] = 5e-2
    VCGL = VCGLoop(rng=rng, Xd=Xd, Xc=Xc, Xm=Xm, Xt=Xt, i_net=IN, \
                 g_net=GN, d_net=DN, chain_len=6, data_dim=data_dim, \
                 prior_dim=PRIOR_DIM, params=vcgl_params)
    VCGL.set_lam_l2w(1e-4)

    out_file = open(RESULT_PATH+"pt_walk_results.txt", 'wb')
    ####################################################
    # Train the VCGLoop by unrolling and applying BPTT #
    ####################################################
    learn_rate = 0.00015
    cost_1 = [0. for i in range(10)]
    for i in range(1000000):
        scale = float(min((i+1), 25000)) / 25000.0
        if ((i+1 % 50000) == 0):
            learn_rate = learn_rate * 0.8
        ########################################
        # TRAIN THE CHAIN IN FREE-RUNNING MODE #
        ########################################
        VCGL.set_all_sgd_params(learn_rate=(scale*learn_rate), \
                mom_1=0.9, mom_2=0.999)
        VCGL.set_disc_weights(dweight_gn=50.0, dweight_dn=50.0)
        VCGL.set_lam_chain_nll(1.0)
        VCGL.set_lam_chain_kld(1.0 + extra_lam_kld)
        VCGL.set_lam_chain_vel(0.0)
        VCGL.set_lam_mask_nll(0.0)
        VCGL.set_lam_mask_kld(0.0)
        # get some data to train with
        tr_idx = npr.randint(low=0,high=tr_samples,size=(batch_size,))
        Xd_batch = Xtr.take(tr_idx, axis=0)
        Xc_batch = 0.0 * Xd_batch
        Xm_batch = 0.0 * Xd_batch
        # do 5 repetitions of the batch
        Xd_batch = np.repeat(Xd_batch, batch_reps, axis=0)
        Xc_batch = np.repeat(Xc_batch, batch_reps, axis=0)
        Xm_batch = np.repeat(Xm_batch, batch_reps, axis=0)
        # examples from the target distribution, to train discriminator
        tr_idx = npr.randint(low=0,high=tr_samples,size=(batch_reps*batch_size,))
        Xt_batch = Xtr.take(tr_idx, axis=0)
        # do a minibatch update of the model, and compute some costs
        outputs = VCGL.train_joint(Xd_batch, Xc_batch, Xm_batch, Xt_batch)
        cost_1 = [(cost_1[k] + 1.*outputs[k]) for k in range(len(outputs))]
        if ((i % 1000) == 0):
            cost_1 = [(v / 1000.0) for v in cost_1]
            o_str_1 = "batch: {0:d}, joint_cost: {1:.4f}, chain_nll_cost: {2:.4f}, chain_kld_cost: {3:.4f}, disc_cost_gn: {4:.4f}, disc_cost_dn: {5:.4f}".format( \
                    i, cost_1[0], cost_1[1], cost_1[2], cost_1[6], cost_1[7])
            print(o_str_1)
            out_file.write(o_str_1+"\n")
            out_file.flush()
            cost_1 = [0. for v in cost_1]
        if ((i % 5000) == 0):
            tr_idx = npr.randint(low=0,high=Xtr.shape[0],size=(5,))
            va_idx = npr.randint(low=0,high=Xva.shape[0],size=(5,))
            Xd_batch = np.vstack([Xtr.take(tr_idx, axis=0), Xva.take(va_idx, axis=0)])
            # draw some chains of samples from the VAE loop
            file_name = RESULT_PATH+"pt_walk_chain_samples_b{0:d}.png".format(i)
            Xd_samps = np.repeat(Xd_batch, 3, axis=0)
            sample_lists = VCGL.GIP.sample_from_chain(Xd_samps, loop_iters=20)
            Xs = np.vstack(sample_lists["data samples"])
            utils.visualize_samples(Xs, file_name, num_rows=20)
            # draw some masked chains of samples from the VAE loop
            file_name = RESULT_PATH+"pt_walk_mask_samples_b{0:d}.png".format(i)
            Xd_samps = np.repeat(Xc_mean[0:Xd_batch.shape[0],:], 3, axis=0)
            Xc_samps = np.repeat(Xd_batch, 3, axis=0)
            Xm_rand = sample_masks(Xc_samps, drop_prob=0.2)
            Xm_patch = sample_patch_masks(Xc_samps, (48,48), (25,25))
            Xm_samps = Xm_rand * Xm_patch
            sample_lists = VCGL.GIP.sample_from_chain(Xd_samps, \
                    X_c=Xc_samps, X_m=Xm_samps, loop_iters=20)
            Xs = np.vstack(sample_lists["data samples"])
            utils.visualize_samples(Xs, file_name, num_rows=20)
            # draw some samples independently from the GenNet's prior
            file_name = RESULT_PATH+"pt_walk_prior_samples_b{0:d}.png".format(i)
            Xs = VCGL.sample_from_prior(20*20)
            utils.visualize_samples(Xs, file_name, num_rows=20)
            # draw discriminator network's weights
            file_name = RESULT_PATH+"pt_walk_dis_weights_b{0:d}.png".format(i)
            utils.visualize_net_layer(VCGL.DN.proto_nets[0][0], file_name)
            # draw inference net first layer weights
            file_name = RESULT_PATH+"pt_walk_inf_weights_b{0:d}.png".format(i)
            utils.visualize_samples(VCGL.IN.W_rica.get_value(borrow=False).T, \
                    file_name, num_rows=20)
            # draw generator net final layer weights
            file_name = RESULT_PATH+"pt_walk_gen_weights_b{0:d}.png".format(i)
            utils.visualize_samples(VCGL.GN.W_rica.get_value(borrow=False), \
                    file_name, num_rows=20)
            #########################
            # Check posterior KLds. #
            #########################
            post_klds = posterior_klds(IN, Xtr, 5000, 5)
            file_name = RESULT_PATH+"pt_walk_post_klds_b{0:d}.png".format(i)
            utils.plot_kde_histogram2( \
                    np.asarray(post_klds), np.asarray(post_klds), file_name, bins=30)
        # DUMP PARAMETERS FROM TIME-TO-TIME
        if (i % 10000 == 0):
            DN.save_to_file(f_name=RESULT_PATH+"pt_walk_params_b{0:d}_DN.pkl".format(i))
            IN.save_to_file(f_name=RESULT_PATH+"pt_walk_params_b{0:d}_IN.pkl".format(i))
            GN.save_to_file(f_name=RESULT_PATH+"pt_walk_params_b{0:d}_GN.pkl".format(i))
    return


def train_recon_from_pretrained_gip(extra_lam_kld=0.0):
    # Simple test code, to check that everything is basically functional.
    print("TESTING...")

    # Initialize a source of randomness
    rng = np.random.RandomState(1234)

    # Load some data to train/validate/test with
    data_file = 'data/tfd_data_48x48.pkl'
    dataset = load_tfd(tfd_pkl_name=data_file, which_set='unlabeled', fold='all')
    Xtr_unlabeled = dataset[0]
    dataset = load_tfd(tfd_pkl_name=data_file, which_set='train', fold='all')
    Xtr_train = dataset[0]
    Xtr = np.vstack([Xtr_unlabeled, Xtr_train])
    dataset = load_tfd(tfd_pkl_name=data_file, which_set='valid', fold='all')
    Xva = dataset[0]
    print("Xtr.shape: {0:s}, Xva.shape: {1:s}".format(str(Xtr.shape),str(Xva.shape)))

    # get and set some basic dataset information
    tr_samples = Xtr.shape[0]
    va_samples = Xva.shape[0]
    data_dim = Xtr.shape[1]
    batch_size = 100
    batch_reps = 5
    prior_sigma = 1.0
    Xtr_mean = np.mean(Xtr, axis=0, keepdims=True)
    Xtr_mean = (0.0 * Xtr_mean) + np.mean(Xtr_mean)
    Xc_mean = np.repeat(Xtr_mean, batch_size, axis=0)

    # Symbolic inputs
    Xd = T.matrix(name='Xd')
    Xc = T.matrix(name='Xc')
    Xm = T.matrix(name='Xm')
    Xt = T.matrix(name='Xt')
    Xp = T.matrix(name='Xp')

    START_FRESH = True
    if START_FRESH:
        ###############################
        # Setup discriminator network #
        ###############################
        # Set some reasonable mlp parameters
        dn_params = {}
        # Set up some proto-networks
        pc0 = [data_dim, (300, 4), (300, 4), 10]
        dn_params['proto_configs'] = [pc0]
        # Set up some spawn networks
        sc0 = {'proto_key': 0, 'input_noise': 0.1, 'bias_noise': 0.1, 'do_dropout': True}
        #sc1 = {'proto_key': 0, 'input_noise': 0.1, 'bias_noise': 0.1, 'do_dropout': True}
        dn_params['spawn_configs'] = [sc0]
        dn_params['spawn_weights'] = [1.0]
        # Set remaining params
        dn_params['init_scale'] = 0.25
        dn_params['lam_l2a'] = 1e-2
        dn_params['vis_drop'] = 0.2
        dn_params['hid_drop'] = 0.5
        # Initialize a network object to use as the discriminator
        DN = PeaNet(rng=rng, Xd=Xd, params=dn_params)
        DN.init_biases(0.0)

        #######################################################
        # Load inferencer and generator from saved parameters #
        #######################################################
        gn_fname = RESULT_PATH+"pt_gip_params_b120000_GN.pkl"
        in_fname = RESULT_PATH+"pt_gip_params_b120000_IN.pkl"
        IN = INet.load_infnet_from_file(f_name=in_fname, rng=rng, Xd=Xd)
        GN = GNet.load_gennet_from_file(f_name=gn_fname, rng=rng, Xp=Xp)
    else:
        ###########################################################
        # Load all networks from partially-trained VCGLoop params #
        ###########################################################
        gn_fname = RESULT_PATH+"pt_walk_params_GN.pkl"
        in_fname = RESULT_PATH+"pt_walk_params_IN.pkl"
        dn_fname = RESULT_PATH+"pt_walk_params_DN.pkl"
        IN = INet.load_infnet_from_file(f_name=in_fname, rng=rng, Xd=Xd)
        GN = GNet.load_gennet_from_file(f_name=gn_fname, rng=rng, Xp=Xp)
        DN = PNet.load_peanet_from_file(f_name=dn_fname, rng=rng, Xd=Xd)

    ###############################
    # Initialize the main VCGLoop #
    ###############################
    vcgl_params = {}
    vcgl_params['lam_l2d'] = 5e-2
    VCGL = VCGLoop(rng=rng, Xd=Xd, Xc=Xc, Xm=Xm, Xt=Xt, i_net=IN, \
                 g_net=GN, d_net=DN, chain_len=5, data_dim=data_dim, \
                 prior_dim=PRIOR_DIM, params=vcgl_params)
    VCGL.set_lam_l2w(1e-4)

    out_file = open(RESULT_PATH+"pt_recon_results.txt", 'wb')
    ####################################################
    # Train the VCGLoop by unrolling and applying BPTT #
    ####################################################
    learn_rate = 0.00015
    cost_2 = [0. for i in range(10)]
    for i in range(1000000):
        scale = float(min((i+1), 25000)) / 25000.0
        if ((i+1 % 50000) == 0):
            learn_rate = learn_rate * 0.66
        #########################################
        # TRAIN THE CHAIN UNDER PARTIAL CONTROL #
        #########################################
        VCGL.set_all_sgd_params(learn_rate=(scale*learn_rate), \
                mom_1=0.9, mom_2=0.999)
        VCGL.set_disc_weights(dweight_gn=40.0, dweight_dn=20.0)
        VCGL.set_lam_chain_nll(0.0)
        VCGL.set_lam_chain_kld(0.0)
        VCGL.set_lam_chain_vel(0.0)
        VCGL.set_lam_mask_nll(1.0)
        VCGL.set_lam_mask_kld(1.0 + extra_lam_kld)
        # get some data to train with
        tr_idx = npr.randint(low=0,high=tr_samples,size=(batch_size,))
        Xd_batch = Xc_mean
        Xc_batch = Xtr.take(tr_idx, axis=0)
        Xm_rand = sample_masks(Xc_batch, drop_prob=0.0)
        Xm_patch = sample_patch_masks(Xc_batch, (48,48), (25,25))
        Xm_batch = Xm_rand * Xm_patch
        tr_idx = npr.randint(low=0,high=tr_samples,size=(batch_size,))
        Xt_batch = Xtr.take(tr_idx, axis=0)
        # do multiple repetitions of the batch
        Xd_batch = np.repeat(Xd_batch, batch_reps, axis=0)
        Xc_batch = np.repeat(Xc_batch, batch_reps, axis=0)
        Xm_batch = np.repeat(Xm_batch, batch_reps, axis=0)
        Xt_batch = np.repeat(Xt_batch, batch_reps, axis=0)
        # do a minibatch update of the model, and compute some costs
        outputs = VCGL.train_joint(Xd_batch, Xc_batch, Xm_batch, Xt_batch)
        if ((i % 2) == 0):
            VCGL.set_lam_chain_nll(1.0)
            VCGL.set_lam_chain_kld(1.0 + extra_lam_kld)
            VCGL.set_lam_chain_vel(0.0)
            VCGL.set_lam_mask_nll(0.0)
            VCGL.set_lam_mask_kld(0.0)
            _outputs = VCGL.train_joint(Xc_batch, 0.*Xc_batch, 0.*Xc_batch, Xt_batch)
        cost_2 = [(cost_2[k] + 1.*outputs[k]) for k in range(len(outputs))]
        if ((i % 1000) == 0):
            cost_2 = [(v / 1000.0) for v in cost_2]
            o_str_2 = "batch {0:d} -- joint_cost: {1:.4f}, mask_nll_cost: {2:.4f}, mask_kld_cost: {3:.4f}, disc_cost_gn: {4:.4f}, disc_cost_dn: {5:.4f}".format( \
                    i, cost_2[0], cost_2[4], cost_2[5], cost_2[6], cost_2[7])
            print(o_str_2)
            out_file.write(o_str_2+"\n")
            out_file.flush()
            cost_2 = [0. for v in cost_2]
        if ((i % 5000) == 0):
            tr_idx = npr.randint(low=0,high=Xtr.shape[0],size=(5,))
            va_idx = npr.randint(low=0,high=Xva.shape[0],size=(5,))
            Xd_batch = np.vstack([Xtr.take(tr_idx, axis=0), Xva.take(va_idx, axis=0)])
            # draw some chains of samples from the VAE loop
            file_name = RESULT_PATH+"pt_recon_chain_samples_b{0:d}.png".format(i)
            Xd_samps = np.repeat(Xd_batch, 3, axis=0)
            sample_lists = VCGL.GIP.sample_from_chain(Xd_samps, loop_iters=20)
            Xs = np.vstack(sample_lists["data samples"])
            utils.visualize_samples(Xs, file_name, num_rows=20)
            # draw some masked chains of samples from the VAE loop
            file_name = RESULT_PATH+"pt_recon_mask_samples_b{0:d}.png".format(i)
            Xd_samps = np.repeat(Xc_mean[0:Xd_batch.shape[0],:], 3, axis=0)
            Xc_samps = np.repeat(Xd_batch, 3, axis=0)
            Xm_rand = sample_masks(Xc_samps, drop_prob=0.2)
            Xm_patch = sample_patch_masks(Xc_samps, (48,48), (25,25))
            Xm_samps = Xm_rand * Xm_patch
            sample_lists = VCGL.GIP.sample_from_chain(Xd_samps, \
                    X_c=Xc_samps, X_m=Xm_samps, loop_iters=20)
            Xs = np.vstack(sample_lists["data samples"])
            utils.visualize_samples(Xs, file_name, num_rows=20)
            # draw some samples independently from the GenNet's prior
            file_name = RESULT_PATH+"pt_recon_prior_samples_b{0:d}.png".format(i)
            Xs = VCGL.sample_from_prior(20*20)
            utils.visualize_samples(Xs, file_name, num_rows=20)
            # draw discriminator network's weights
            file_name = RESULT_PATH+"pt_recon_dis_weights_b{0:d}.png".format(i)
            utils.visualize_net_layer(VCGL.DN.proto_nets[0][0], file_name)
            # draw inference net first layer weights
            file_name = RESULT_PATH+"pt_recon_inf_weights_b{0:d}.png".format(i)
            utils.visualize_net_layer(VCGL.IN.shared_layers[0], file_name)
            # draw generator net final layer weights
            file_name = RESULT_PATH+"pt_recon_gen_weights_b{0:d}.png".format(i)
            if GN.out_type == 'sigmoid':
                utils.visualize_net_layer(VCGL.GN.mlp_layers[-1], file_name, use_transpose=True)
            else:
                utils.visualize_net_layer(VCGL.GN.mlp_layers[-2], file_name, use_transpose=True)
            #########################
            # Check posterior KLds. #
            #########################
            post_klds = posterior_klds(IN, Xtr, 5000, 5)
            file_name = RESULT_PATH+"pt_recon_post_klds_b{0:d}.png".format(i)
            utils.plot_kde_histogram2( \
                    np.asarray(post_klds), np.asarray(post_klds), file_name, bins=30)
        # DUMP PARAMETERS FROM TIME-TO-TIME
        if (i % 10000 == 0):
            DN.save_to_file(f_name=RESULT_PATH+"pt_recon_params_b{0:d}_DN.pkl".format(i))
            IN.save_to_file(f_name=RESULT_PATH+"pt_recon_params_b{0:d}_IN.pkl".format(i))
            GN.save_to_file(f_name=RESULT_PATH+"pt_recon_params_b{0:d}_GN.pkl".format(i))
    return

if __name__=="__main__":
    # FOR EXTREME KLD REGULARIZATION
	pretrain_gip(extra_lam_kld=59.0, kld2_scale=0.0)
	train_walk_from_pretrained_gip(extra_lam_kld=59.0)

    # FOR KLD MODEL
    # pretrain_gip(extra_lam_kld=4.0, kld2_scale=0.1)
    # train_walk_from_pretrained_gip(extra_lam_kld=4.0)
    # train_recon_from_pretrained_gip(extra_lam_kld=4.0)

    # FOR VAE MODEL
    # pretrain_gip(extra_lam_kld=0.0, kld2_scale=0.0)
    # train_walk_from_pretrained_gip(extra_lam_kld=0.0)
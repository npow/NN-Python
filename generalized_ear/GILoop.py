###################################################################
# Code for managing and training a generator/discriminator pair.  #
###################################################################

# basic python
import numpy as np
import numpy.random as npr
from collections import OrderedDict

# theano business
import theano
import theano.tensor as T
from theano.ifelse import ifelse
import theano.tensor.shared_randomstreams
#from theano.sandbox.cuda.rng_curand import CURAND_RandomStreams

# phil's sweetness
from NetLayers import HiddenLayer, DiscLayer
from GenNet import projected_moments

class GILoop(object):
    """
    Controller for propagating through a generate<->inference loop.

    The generator must be an instance of the GEN_NET class implemented in
    "GINets.py". The discriminator must be an instance of the EarNet class,
    as implemented in "EarNet.py".

    Parameters:
        rng: numpy.random.RandomState (for reproducibility)
        g_net: The GenNet instance that will serve as the base generator
        i_net: The InfNet instance that will serve as the base inferer
        loop_iters: The number of loop cycles to unroll
    """
    def __init__(self, rng=None, g_net=None, i_net=None, data_dim=None, \
            latent_dim=None, loop_iters=1):
        # Do some stuff!
        self.rng = theano.tensor.shared_randomstreams.RandomStreams( \
                rng.randint(100000))
        self.GN_base = g_net
        self.IN_base = i_net
        self.data_dim = data_dim
        self.latent_dim = latent_dim
        self.loop_iters = loop_iters

        # check that various dimensions are set coherently
        assert(self.latent_dim == self.GN_base.mlp_layers[0].in_dim)
        assert(self.latent_dim == self.IN_base.mu_layers[-1].out_dim)
        assert(self.latent_dim == self.IN_base.sigma_layers[-1].out_dim)
        assert(self.data_dim == self.GN_base.mlp_layers[-1].out_dim)
        assert(self.data_dim == self.IN_base.shared_layers[0].in_dim)

        # symbolic var data input
        self.Xd = T.matrix(name='gil_Xd')
        # symbolic var noise input
        self.Xn = T.matrix(name='gil_Xn')
        # symbolic mask input
        self.Xm = T.matrix(name='gil_Xm')

        # shared var learning rate for generator and discriminator
        zero_ary = np.zeros((1,)).astype(theano.config.floatX)
        self.lr_gn = theano.shared(value=zero_ary, name='gil_lr_gn')
        self.lr_in = theano.shared(value=zero_ary, name='gil_lr_in')
        # shared var momentum parameters for generator and discriminator
        self.mo_gn = theano.shared(value=zero_ary, name='gil_mo_gn')
        self.mo_in = theano.shared(value=zero_ary, name='gil_mo_in')
        # init parameters for controlling learning dynamics
        self.set_gn_sgd_params() # init SGD rate/momentum for GN
        self.set_in_sgd_params() # init SGD rate/momentum for IN

        #######################################################
        # Welcome to: Moment Matching Cost Information Center #
        #######################################################
        #
        # Get parameters for managing the moment matching cost. The moment
        # matching is based on exponentially-decaying estimates of the mean
        # and covariance of the distribution induced by the generator network
        # and the (latent) noise being fed to it.
        #
        # We provide the option of performing moment matching with either the
        # raw generator output, or with linearly-transformed generator output.
        # Either way, the given target mean and covariance should have the
        # appropriate dimension for the space in which we'll be matching the
        # generator's 1st/2nd moments with the target's 1st/2nd moments. For
        # clarity, the computation we'll perform looks like:
        #
        #   Xm = X - np.mean(X, axis=0)
        #   XmP = np.dot(Xm, P)
        #   C = np.dot(XmP.T, XmP)
        #
        # where Xm is the mean-centered samples from the generator and P is
        # the matrix for the linear transform to apply prior to computing
        # the moment matching cost. For simplicity, the above code ignores the
        # use of an exponentially decaying average to track the estimated mean
        # and covariance of the generator's output distribution.
        #
        # The relative contribution of the current batch to these running
        # estimates is determined by self.mom_mix_rate. The mean estimate is
        # first updated based on the current batch, then the current batch
        # is centered with the updated mean, then the covariance estimate is
        # updated with the mean-centered samples in the current batch.
        #
        # Strength of the moment matching cost is given by self.mom_match_cost.
        # Target mean/covariance are given by self.target_mean/self.target_cov.
        # If a linear transform is to be applied prior to matching, it is given
        # by self.mom_match_proj.
        #
        zero_ary = np.zeros((1,))
        mmr = zero_ary + params['mom_mix_rate']
        self.mom_mix_rate = theano.shared(name='gil_mom_mix_rate', \
            value=mmr.astype(theano.config.floatX))
        mmw = zero_ary + params['mom_match_weight']
        self.mom_match_weight = theano.shared(name='gil_mom_match_weight', \
            value=mmw.astype(theano.config.floatX))
        targ_mean = params['target_mean'].astype(theano.config.floatX)
        targ_cov = params['target_cov'].astype(theano.config.floatX)
        assert(targ_mean.size == targ_cov.shape[0]) # mean and cov use same dim
        assert(targ_cov.shape[0] == targ_cov.shape[1]) # cov must be square
        self.target_mean = theano.shared(value=targ_mean, name='gil_target_mean')
        self.target_cov = theano.shared(value=targ_cov, name='gil_target_cov')
        mmp = np.identity(targ_cov.shape[0]) # default to identity transform
        if 'mom_match_proj' in params:
            mmp = params['mom_match_proj'] # use a user-specified transform
        assert(mmp.shape[0] == self.data_dim) # transform matches data dim
        assert(mmp.shape[1] == targ_cov.shape[0]) # and matches mean/cov dims
        mmp = mmp.astype(theano.config.floatX)
        self.mom_match_proj = theano.shared(value=mmp, name='gcp_mom_map_proj')
        # finally, we can construct the moment matching cost! and the updates
        # for the running mean/covariance estimates too!
        self.mom_match_cost, self.mom_updates = self._construct_mom_stuff()
        #########################################
        # Thank you for visiting the M.M.C.I.C. #
        #########################################

        # Grab the full set of "optimizable" parameters from the generator
        # and inference networks that we'll be working with.
        self.in_params = [p for p in self.IN.mlp_params]
        self.gn_params = [p for p in self.GN.mlp_params]

        # TODO: construct a working generate <-> infer loop


        # TODO: construct the cost functions for gen <-> inf loop
        self.in_cost = self.vari_cost_in + self.IN.act_reg_cost
        self.gn_cost = self.vari_cost_gn + self.GN.act_reg_cost

        # Initialize momentums for mini-batch SGD updates. All parameters need
        # to be safely nestled in their lists by now.
        self.joint_moms = OrderedDict()
        self.in_moms = OrderedDict()
        self.gn_moms = OrderedDict()
        for p in self.gn_params:
            p_mo = np.zeros(p.get_value(borrow=True).shape)
            self.gn_moms[p] = theano.shared(value=p_mo.astype(theano.config.floatX))
            self.joint_moms[p] = self.gn_moms[p]
        for p in self.in_params:
            p_mo = np.zeros(p.get_value(borrow=True).shape)
            self.in_moms[p] = theano.shared(value=p_mo.astype(theano.config.floatX))
            self.joint_moms[p] = self.in_moms[p]

        # Construct the updates for the generator and inferer networks
        self.joint_updates = OrderedDict()
        self.gn_updates = OrderedDict()
        self.in_updates = OrderedDict()
        for var in self.in_params:
            # these updates are for trainable params in the discriminator net...
            # first, get gradient of cost w.r.t. var
            var_grad = T.grad(self.in_cost, var)
            # get the momentum for this var
            var_mom = self.in_moms[var]
            # update the momentum for this var using its grad
            self.in_updates[var_mom] = (self.mo_in[0] * var_mom) + \
                    ((1.0 - self.mo_in[0]) * var_grad)
            self.joint_updates[var_mom] = self.in_updates[var_mom]
            # make basic update to the var
            var_new = var - (self.lr_in[0] * var_mom)
            if ((var in self.IN.clip_params) and \
                    (var in self.IN.clip_norms) and \
                    (self.IN.clip_params[var] == 1)):
                # clip the basic updated var if it is set as clippable
                clip_norm = self.IN.clip_norms[var]
                var_norms = T.sum(var_new**2.0, axis=1, keepdims=True)
                var_scale = T.clip(T.sqrt(clip_norm / var_norms), 0., 1.)
                self.in_updates[var] = var_new * var_scale
            else:
                # otherwise, just use the basic updated var
                self.in_updates[var] = var_new
            # add this var's update to the joint updates too
            self.joint_updates[var] = self.in_updates[var]
        for var in self.mom_updates:
            # these updates are for the generator distribution's running first
            # and second-order moment estimates
            self.gn_updates[var] = self.mom_updates[var]
            self.joint_updates[var] = self.gn_updates[var]
        for var in self.gn_params:
            # these updates are for trainable params in the generator net...
            # first, get gradient of cost w.r.t. var
            var_grad = T.grad(self.gn_cost, var, \
                    consider_constant=[self.GN.dist_mean, self.GN.dist_cov])
            # get the momentum for this var
            var_mom = self.gn_moms[var]
            # update the momentum for this var using its grad
            self.gn_updates[var_mom] = (self.mo_gn[0] * var_mom) + \
                    ((1.0 - self.mo_gn[0]) * var_grad)
            self.joint_updates[var_mom] = self.gn_updates[var_mom]
            # make basic update to the var
            var_new = var - (self.lr_gn[0] * var_mom)
            if ((var in self.GN.clip_params) and \
                    (var in self.GN.clip_norms) and \
                    (self.GN.clip_params[var] == 1)):
                # clip the basic updated var if it is set as clippable
                clip_norm = self.GN.clip_norms[var]
                var_norms = T.sum(var_new**2.0, axis=1, keepdims=True)
                var_scale = T.clip(T.sqrt(clip_norm / var_norms), 0., 1.)
                self.gn_updates[var] = var_new * var_scale
            else:
                # otherwise, just use the basic updated var
                self.gn_updates[var] = var_new
            # add this var's update to the joint updates too
            self.joint_updates[var] = self.gn_updates[var]

        # Construct batch-based training functions for the generator and
        # inferer networks, as well as a joint training function.
        self.train_gn = self._construct_train_gn()
        self.train_in = self._construct_train_in()
        self.train_joint = self._construct_train_joint()

        # Construct a function for computing the ouputs of the generator
        # network for a batch of noise. Presumably, the noise will be drawn
        # from the same distribution that was used in training....
        self.sample_from_gn = self._construct_gn_sampler()
        return

    def set_gn_sgd_params(self, learn_rate=0.02, momentum=0.9):
        """
        Set learning rate and momentum parameter for generator updates.
        """
        zero_ary = np.zeros((1,))
        new_lr = zero_ary + learn_rate
        self.lr_gn.set_value(new_lr.astype(theano.config.floatX))
        new_mo = zero_ary + momentum
        self.mo_gn.set_value(new_mo.astype(theano.config.floatX))
        return

    def set_in_sgd_params(self, learn_rate=0.02, momentum=0.9):
        """
        Set learning rate and momentum parameter for discriminator updates.
        """
        zero_ary = np.zeros((1,))
        new_lr = zero_ary + learn_rate
        self.lr_in.set_value(new_lr.astype(theano.config.floatX))
        new_mo = zero_ary + momentum
        self.mo_in.set_value(new_mo.astype(theano.config.floatX))
        return

    def _construct_train_gn(self):
        """
        Construct theano function to train generator on its own.
        """
        outputs = [self.mom_match_cost, self.disc_cost_gn, self.disc_cost_dn]
        func = theano.function(inputs=[ self.Xd, self.Xn, self.Id, self.In ], \
                outputs=outputs, \
                updates=self.gn_updates, \
                givens={self.input_data: self.Xd, \
                        self.input_noise: self.Xn})
        theano.printing.pydotprint(func, \
            outfile='gn_func_graph.png', compact=True, format='png', with_ids=False, \
            high_contrast=True, cond_highlight=None, colorCodes=None, \
            max_label_size=70, scan_graphs=False, var_with_name_simple=False, \
            print_output_file=True, assert_nb_all_strings=-1)
        return func

    def _construct_train_in(self):
        """
        Construct theano function to train inferer on its own.
        """
        outputs = [self.mom_match_cost, self.disc_cost_gn, self.disc_cost_dn]
        func = theano.function(inputs=[ self.Xd, self.Xn, self.Id, self.In ], \
                outputs=outputs, \
                updates=self.dn_updates, \
                givens={self.input_data: self.Xd, \
                        self.input_noise: self.Xn})
        theano.printing.pydotprint(func, \
            outfile='dn_func_graph.png', compact=True, format='png', with_ids=False, \
            high_contrast=True, cond_highlight=None, colorCodes=None, \
            max_label_size=70, scan_graphs=False, var_with_name_simple=False, \
            print_output_file=True, assert_nb_all_strings=-1)
        return func

    def _construct_train_joint(self):
        """
        Construct theano function to train generator and inferer jointly.
        """
        outputs = [self.mom_match_cost, self.disc_cost_gn, self.disc_cost_dn]
        func = theano.function(inputs=[ self.Xd, self.Xn, self.Id, self.In ], \
                outputs=outputs, \
                updates=self.joint_updates, \
                givens={self.input_data: self.Xd, \
                        self.input_noise: self.Xn})
        return func

    def _construct_gn_sampler(self):
        """
        Construct theano function to sample from the generator network.
        """
        Xn_sym = T.matrix('gn_sampler_input')
        theano_func = theano.function( \
               inputs=[ Xn_sym ], \
               outputs=[ self.GN_base.output ], \
               givens={ self.GN_base.input_var: Xn_sym })
        sample_func = lambda Xn: theano_func(Xn)[0]
        return sample_func

if __name__=="__main__":
    NOT_DONE = True

    print("TESTING COMPLETE!")




##############
# EYE BUFFER #
##############

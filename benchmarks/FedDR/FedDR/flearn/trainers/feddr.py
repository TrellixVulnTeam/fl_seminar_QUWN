import numpy as np
from tqdm import trange, tqdm
import tensorflow as tf

from flearn.optimizer.pgd import PerturbedGradientDescent
from flearn.utils.tf_utils import process_grad, process_sparse_grad

from flearn.models.client_dr import Client_DR
from flearn.utils.model_utils import Metrics
from flearn.utils.utils import History
import copy
from flearn.regularizers import REGISTRY as regularizers_REGISTRY

class Server(object):
    def __init__(self, params, learner, dataset):
        '''initialize local models, build comp. graph'''

        # save input arguments
        for key, val in params.items(): setattr(self, key, val);

        # get number of clients
        users, _, _, _ = dataset
        self.num_clients = len(users)
        if self.clients_per_round < 1: 
            self.clients_per_round = self.num_clients

        # reset comp. graph
        tf.reset_default_graph()
        self.inner_opt_list = [PerturbedGradientDescent(params['learning_rate'], 1.0/params['eta']) for _ in range(self.num_clients+1)]
        
        # create server and clients' models
        self.server_model = learner(*params['model_params'], self.inner_opt_list[-1], self.seed)
        self.client_train_model_list = []
        for i in range(self.num_clients):
            self.client_train_model_list.append(learner(*params['model_params'], self.inner_opt_list[i], self.seed))

        # set up clients
        self.options = {
            'eta': params.get('eta', 1.0),
            'alpha': params.get('alpha', 1.9),
            'reg_coeff': params.get('reg_coeff', 1.)
        }
        self.clients = self.setup_clients(dataset, self.client_train_model_list)
        print('{} Clients in Total'.format(len(self.clients)))

        # initialize server model params
        self.latest_model = self.server_model.get_params()
        self.y_model = [np.zeros_like(x) for x in self.latest_model]
        self.x_hat_model = [np.zeros_like(x) for x in self.latest_model]

        # setup regularizers
        self.reg_type = params.get('reg_type', None)
        if self.reg_type in regularizers_REGISTRY.keys():
            self.reg_function = regularizers_REGISTRY[self.reg_type](params.get('reg_coeff'))
        else:
            raise ValueError('Regularizer not supported!')

        # compute model length
        self.model_len = process_grad(self.latest_model).size

        # finalize comp. graph
        for i in range(self.num_clients):
            self.clients[i].model.sess.graph.finalize()
        self.server_model.sess.graph.finalize()
        

    def __del__(self):
        '''clean up models'''
        self.server_model.close()
        for train_model in self.client_train_model_list:
            train_model.close()

    def setup_clients(self, dataset, train_model_list=None):
        '''instantiates clients based on given train and test data directories

        Return:
            list of Clients
        '''
        users, groups, train_data, test_data = dataset
        if len(groups) == 0:
            groups = [None for _ in users]
        all_clients = [Client_DR(u, g, train_data[u], test_data[u], 
                        train_model, options=self.options) for u, g, train_model in 
                            zip(users, groups, train_model_list)]
        return all_clients

    def get_clients_stats(self, model):
        '''compute clients' statistics

        Args:
            model: latest model

        Return:
            train and test stats, local and global grads
        '''
        num_samples_train = []
        num_samples_test = []
        tot_correct_train = []
        tot_correct_test = []
        losses = []
        local_grads = []

        global_grads = np.zeros(self.model_len)

        for c in self.clients:
            # get a copy of current model
            client_params = c.get_params()
            c.set_params(model)

            ct_train, cl_train, ns_train = c.train_error_and_loss()
            ct_test, ns_test = c.test()
            _, client_grad = c.get_grads(self.model_len)

            tot_correct_train.append(ct_train*1.0)
            tot_correct_test.append(ct_test*1.0)
            num_samples_train.append(ns_train)
            num_samples_test.append(ns_test)
            losses.append(cl_train*1.0)
            local_grads.append(client_grad)
            global_grads = np.add(global_grads, client_grad * ns_train)

            # return current model
            c.set_params(client_params)

        global_grads = global_grads * 1.0 / np.sum(np.asarray(num_samples_train))
        
        ids = [c.id for c in self.clients]
        groups = [c.group for c in self.clients]

        stats_train = ids, groups, num_samples_train, tot_correct_train, losses
        stats_test = ids, groups, num_samples_test, tot_correct_test

        return stats_train, stats_test, global_grads, local_grads
 
    def save(self):
        pass

    def select_clients(self, round, num_clients=20):
        '''selects num_clients clients weighted by number of samples from possible_clients
        
        Args:
            num_clients: number of clients to select; default 20
                note that within function, num_clients is set to
                min(num_clients, len(possible_clients))
        
        Return:
            list of selected clients objects
        '''
        num_clients = min(num_clients, self.num_clients)

        np.random.seed(round)  # make sure for each comparison, we are selecting the same clients each round
        indices = np.random.choice(range(self.num_clients), num_clients, replace=False)
        return indices, np.asarray(self.clients)[indices]

    def aggregate(self, wsolns):
        '''aggregate local solutions
        
        Args:
            wsolns: a list of local model params
        
        Return:
            averaged model params
        '''
        total_weight = 0.0
        base = [0]*len(wsolns[0][1])
        for (w, soln) in wsolns:  # w is the number of local samples
            total_weight += w
            for i, v in enumerate(soln):
                base[i] += w*v.astype(np.float64)

        averaged_soln = [v / total_weight for v in base]

        return averaged_soln
   
    def train(self):
        '''Train using Federated Douglas-Rachford algorithm'''
        print('Training using FedDR with {} workers ---'.format(self.clients_per_round))
        history = History(['ComRound','TrainLoss','GradNorm','TrainAcc','TestAcc','GradDiff','NumBytes'])

        tqdm.write(' ---------------------------------------------------------------------------------------')
        tqdm.write('| Com. Round | Train Loss | Grad Norm | Train Acc. | Test Acc. | Grad Diff. | Num Bytes |')
        tqdm.write(' ---------------------------------------------------------------------------------------')

        total_bytes_r = 0
        for k in range(self.num_rounds):
            # test model
            if k % self.eval_every == 0:
                stats_train, stats, global_grads, local_grads = self.get_clients_stats(self.latest_model)

                grad_norm = np.sqrt(np.sum(np.square(global_grads)))

                difference = 0
                for idx in range(len(self.clients)):
                    difference += np.sum(np.square(global_grads - local_grads[idx]))
                difference = difference * 1.0 / self.num_clients

                train_acc = np.sum(stats_train[3])*1.0/np.sum(stats_train[2])
                test_acc = np.sum(stats[3])*1.0/np.sum(stats[2])
                train_loss = np.dot(stats_train[4], stats_train[2])*1.0/np.sum(stats_train[2]) + np.sum([self.reg_function.func_eval(x) for x in self.latest_model])
                tqdm.write('| {:10d} | {:10.5f} | {:9.2e} | {:10.5f} | {:9.5f} | {:10.2e} | {:9.2e} |'.format(k, train_loss, grad_norm, train_acc, test_acc, difference, total_bytes_r))
                history.update([k, train_loss, grad_norm, train_acc, test_acc, difference, total_bytes_r])

            indices, selected_clients = self.select_clients(k, num_clients=self.clients_per_round)  # uniform sampling

            csolns = [] # buffer for receiving client solutions

            for idx, c in enumerate(self.clients):

                if idx in indices:
                    # communicate the latest model
                    c.set_params(self.latest_model)

                    # solve minimization locally
                    soln, stats = c.solve_inner(num_epochs=self.num_epochs, batch_size=self.batch_size, update=True)

                    # update num bytes r
                    total_bytes_r += stats[2]
                    csolns.append(soln)

                else:
                    soln, stats = c.solve_inner(num_epochs=self.num_epochs, batch_size=self.batch_size, update=False)                    
                    # gather solutions from client
                    csolns.append(soln)

            # perform aggregation
            self.x_hat_model = self.aggregate(csolns)

            # proximal step
            for i in range(len(self.latest_model)):
                self.latest_model[i] = self.reg_function.prox_eval( self.x_hat_model[i], self.options['eta'] )

            # update server model for metric calculation
            self.server_model.set_params(self.latest_model)

        # final test model
        stats_train, stats, global_grads, local_grads = self.get_clients_stats(self.latest_model)

        grad_norm = np.sqrt(np.sum(np.square(global_grads)))

        difference = 0
        for idx in range(self.num_clients):
            difference += np.sum(np.square(global_grads - local_grads[idx]))
        difference = difference * 1.0 / self.num_clients

        train_acc = np.sum(stats_train[3])*1.0/np.sum(stats_train[2])
        test_acc = np.sum(stats[3])*1.0/np.sum(stats[2])
        train_loss = np.dot(stats_train[4], stats_train[2])*1.0/np.sum(stats_train[2]) + np.sum([self.reg_function.func_eval(x) for x in self.latest_model])
        tqdm.write('| {:10d} | {:10.5f} | {:9.2e} | {:10.5f} | {:9.5f} | {:10.2e} | {:9.2e} |'.format(self.num_rounds, train_loss, grad_norm, train_acc, test_acc, difference, total_bytes_r))
        tqdm.write(' ---------------------------------------------------------------------------------------')
        history.update([self.num_rounds, train_loss, grad_norm, train_acc, test_acc, difference, total_bytes_r])
        
        return history.get_history(dataframe=True)


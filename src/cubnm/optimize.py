"""
Optimizers of the model free parameters
"""
import os
import itertools
from abc import ABC, abstractmethod
import json
import pickle
import numpy as np
import pandas as pd
from pymoo.core.problem import Problem
from pymoo.algorithms.soo.nonconvex.cmaes import CMAES
from pymoo.algorithms.moo.nsga2 import NSGA2
from pymoo.termination import get_termination
import cma
import skopt

from cubnm import sim


class GridSearch:
    # TODO: GridSearch should also be an Optimizer
    def __init__(self, model, params, **kwargs):
        """
        Grid search of model free parameters

        Parameters
        ---------
        model : :obj:`str`, {'rWW', 'rWWEx', 'Kuramoto'}
        params : :obj:`dict` of :obj:`tuple` or :obj:`float`
            a dictionary including parameter names as keys and their
            fixed values (:obj:`float`) or discrete range of
            values (:obj:`tuple` of (min, max, n)) as values.
        **kwargs
            Keyword arguments passed to :class:`cubnm.sim.SimGroup`

        Example
        -------
        Run a grid search of rWW model with 10 G and 10 wEE values
        with fixed wEI: ::

            from cubnm import datasets, optimize
        
            gs = optimize.GridSearch(
                model = 'rWW',
                params = {
                    'G': (0.5, 2.5, 10),
                    'wEE': (0.05, 0.75, 10),
                    'wEI': 0.21
                },
                duration = 60,
                TR = 1,
                sc_path = datasets.load_sc('strength', 'schaefer-100', return_path=True),
                states_ts = True
            )
            emp_fc_tril = datasets.load_functional('FC', 'schaefer-100', exc_interhemispheric=True)
            emp_fcd_tril = datasets.load_functional('FCD', 'schaefer-100', exc_interhemispheric=True)
            scores = gs.evaluate(emp_fc_tril, emp_fcd_tril)
        """
        self.model = model
        sim_group_cls = getattr(sim, f"{self.model}SimGroup")
        self.sim_group = sim_group_cls(**kwargs)
        param_ranges = {}
        for param, v in params.items():
            if isinstance(v, tuple):
                param_ranges[param] = np.linspace(v[0], v[1], v[2])
            else:
                param_ranges[param] = np.array([v])
        self.param_combs = pd.DataFrame(
            list(itertools.product(*param_ranges.values())),
            columns=param_ranges.keys(),
            dtype=float,
        )
        self.sim_group.N = self.param_combs.shape[0]
        for param in params.keys():
            self.sim_group.param_lists[param] = []
            for sim_idx in range(self.sim_group.N):
                if param in (self.sim_group.global_param_names+["v"]):
                    # global parameters have (sims,) shape
                    self.sim_group.param_lists[param].append(
                        self.param_combs.iloc[sim_idx].loc[param]
                    )
                else:
                    # regional parameters have (sims, nodes) shape
                    # but the same value for each node is repeated
                    self.sim_group.param_lists[param].append(
                        np.repeat(
                            self.param_combs.iloc[sim_idx].loc[param],
                            self.sim_group.nodes,
                        )
                    )
            self.sim_group.param_lists[param] = np.array(
                self.sim_group.param_lists[param]
            )

    def evaluate(self, emp_fc_tril, emp_fcd_tril):
        """
        Runs the grid simulations and evaluates their
        goodness of fit to the empirical FC and FCD

        Parameters
        ----------
        emp_fc_tril : :obj:`np.ndarray`
            lower triangular part of empirical FC. Shape: (edges,)
        emp_fcd_tril : :obj:`np.ndarray`
            lower triangular part of empirical FCD. Shape: (window_pairs,)

        Returns
        -------
        :obj:`pd.DataFrame`
            The goodness of fit measures (columns) of each simulation (rows)
        """
        self.sim_group.run()
        return self.sim_group.score(emp_fc_tril, emp_fcd_tril)


class BNMProblem(Problem):
    def __init__(
        self,
        model,
        params,
        emp_fc_tril,
        emp_fcd_tril,
        het_params=[],
        maps=None,
        maps_coef_range='auto',
        node_grouping=None,
        multiobj=False,
        **kwargs,
    ):
        """
        Biophysical network model problem. A :class:`pymoo.core.problem.Problem` 
        that defines the model, free parameters and their ranges, and target empirical
        data (FC and FCD), and the simulation configurations (through 
        :class:`cubnm.sim.SimGroup`). 
        :class:`cubnm.optimize.Optimizer` classes can be
        used to optimize the free parameters of this problem.

        Parameters
        ----------
        model : :obj:`str`, {'rWW', 'rWWEx', 'Kuramoto'}
        params : :obj:`dict` of :obj:`tuple` or :obj:`float`
            a dictionary including parameter names as keys and their
            fixed values (:obj:`float`) or continuous range of
            values (:obj:`tuple` of (min, max)) as values.
        emp_fc_tril : :obj:`np.ndarray`
            lower triangular part of empirical FC. Shape: (edges,)
        emp_fcd_tril : :obj:`np.ndarray`
            lower triangular part of empirical FCD. Shape: (window_pairs,)
        het_params : :obj:`list` of :obj:`str`, optional
            which regional parameters are heterogeneous across nodes
        maps : :obj:`str`, optional
            path to heterogeneity maps as a text file or a numpy array.
            Shape: (n_maps, nodes).
            If provided one free parameter per regional parameter per 
            each map will be added.
        maps_coef_range : 'auto' or :obj:`tuple` or :obj:`list` of :obj:`tuple`
            - 'auto': uses (-1/max, -1/min) for maps with positive and negative
                values (assuming they are z-scored) and (0, 1) otherwise
            - :obj:`tuple`: uses the same range for all maps
            - :obj:`list` of :obj:`tuple`: n-map element list specifying the range
                of coefficients for each map
        node_grouping : {None, 'node', 'sym', :obj:`str`, :obj:`np.ndarray`}, optional
            - None: does not use region-/group-specific parameters
            - 'node': each node has its own regional free parameters
            - 'sym': uses the same regional free parameters for each pair of symmetric nodes
                (e.g. L and R hemispheres). Assumes symmetry  of parcels between L and R
                hemispheres.
            - :obj:`str`: path to a text file including node grouping array. Shape: (nodes,)
            - :obj:`np.ndarray`: a numpy array. Shape: (nodes,)
        multiobj : :obj:`bool`, optional
            instead of combining the objectives into a single objective function
            (via summation) defines each objective separately. This must not be used
            with single-objective optimizers
        **kwargs
            Keyword arguments passed to :class:`cubnm.sim.SimGroup`
        """
        # set opts
        self.model = model 
        self.params = params
        self.emp_fc_tril = emp_fc_tril
        self.emp_fcd_tril = emp_fcd_tril
        self.het_params = kwargs.pop("het_params", het_params)
        self.input_maps = kwargs.pop("maps", maps)
        self.maps_coef_range = kwargs.pop("maps_coef_range", maps_coef_range)
        self.reject_negative = False # not implemented yet
        self.node_grouping = kwargs.pop("node_grouping", node_grouping)
        self.multiobj = kwargs.pop("multiobj", multiobj)
        # initialize sim_group (N not known yet)
        sim_group_cls = getattr(sim, f"{self.model}SimGroup")
        self.sim_group = sim_group_cls(**kwargs)
        # raise errors if opts are impossible
        if (self.model == 'rWW') & (self.sim_group.do_fic) & ("wIE" in self.het_params):
            raise ValueError(
                "In rWW wIE should not be specified as a heterogeneous parameter when FIC is done"
            )
        if (self.node_grouping is not None) & (self.input_maps is not None):
            raise ValueError("Both `node_grouping` and `maps` cannot be used")
        # identify free and fixed parameters
        self.free_params = []
        self.lb = []
        self.ub = []
        self.global_params = self.sim_group.global_param_names + ["v"]
        self.regional_params = self.sim_group.regional_param_names
        # decide if parameters can be variable across nodes/groups
        # note that this is different from map-based heterogeneity
        self.is_regional = self.node_grouping is not None
        if self.is_regional:
            for param in self.het_params:
                if not isinstance(params[param], tuple):
                    raise ValueError(
                        f"{param} is set to be heterogeneous based on groups but no range is provided"
                    )
        # set up node_groups (ordered index of all unique groups)
        # and memberships (from node i to N, which group they belong to)
        if self.is_regional:
            if self.node_grouping == "node":
                # each node gets its own regional free parameters
                # therefore each node has its own group and
                # is the only member of it
                self.node_groups = np.arange(self.sim_group.nodes)
                self.memberships = np.arange(self.sim_group.nodes)
            elif self.node_grouping == "sym":
                print(
                    "Warning: `sym` node grouping assumes symmetry of parcels between L and R hemispheres"
                )
                # nodes i & rh_idx+i belong to the same group
                # and will have similar parameters
                assert self.sim_group.nodes % 2 == 0, "Number of nodes must be even"
                rh_idx = int(self.sim_group.nodes / 2)
                self.node_groups = np.arange(rh_idx)
                self.memberships = np.tile(np.arange(rh_idx), 2)
            else:
                if isinstance(self.node_grouping, (str, os.PathLike)):
                    self.memberships = np.loadtxt(self.node_grouping).astype("int")
                else:
                    self.memberships = self.node_grouping.astype("int")
                self.node_groups = np.unique(self.memberships)
        # set up global and regional (incl. bias) free parameters
        for param, v in params.items():
            if isinstance(v, tuple):
                if (
                    self.is_regional
                    & (param in self.regional_params)
                    & (param in self.het_params)
                ):
                    # set up regional parameters which are regionally variable based on groups
                    for group in self.node_groups:
                        param_name = f"{param}{group}"
                        self.free_params.append(param_name)
                        self.lb.append(v[0])
                        self.ub.append(v[1])
                else:
                    # is a global or bias parameter
                    self.free_params.append(param)
                    self.lb.append(v[0])
                    self.ub.append(v[1])
        self.fixed_params = list(set(self.params.keys()) - set(self.free_params))
        # set up map scaler parameters
        self.is_heterogeneous = False
        if self.input_maps is not None:
            self.is_heterogeneous = True
            if isinstance(self.input_maps, (str, os.PathLike)):
                self.maps = np.loadtxt(self.input_maps)
            else:
                self.maps = self.input_maps
            assert (
                self.maps.shape[1] == self.sim_group.nodes
            ), f"Maps second dimension {self.maps.shape[1]} != nodes {self.sim_group.nodes}"
            for param in self.het_params:
                for map_idx in range(self.maps.shape[0]):
                    if self.maps_coef_range == 'auto':
                        # identify the scaler range
                        map_max = self.maps[map_idx, :].max()
                        map_min = self.maps[map_idx, :].min()
                        if (map_min < 0) & (map_max > 0):
                            # e.g. z-scored
                            scale_min = -1 / map_max
                            scale_min = np.ceil(scale_min / 0.1) * 0.1  # round up
                            scale_max = -1 / map_min
                            scale_max = np.floor(scale_max / 0.1) * 0.1  # round down
                        else:
                            scale_min = 0.0
                            scale_max = 1.0
                    elif isinstance(self.maps_coef_range, tuple):
                        scale_min, scale_max = self.maps_coef_range
                    elif isinstance(self.maps_coef_range, list):
                        scale_min, scale_max = self.maps_coef_range[map_idx]
                    else:
                        raise ValueError("Invalid maps_coef_range provided")
                    # add the scaler as a free parameter
                    scaler_name = f"{param}scale{map_idx}"
                    self.free_params.append(scaler_name)
                    self.lb.append(scale_min)
                    self.ub.append(scale_max)
        # convert bounds to arrays
        self.lb = np.array(self.lb)
        self.ub = np.array(self.ub)
        # determine ndim of the problem
        self.ndim = len(self.free_params)
        # determine the number of objectives
        if self.multiobj:
            self.obj_names = []
            for term in self.sim_group.gof_terms:
                # these are cost values which are minimized by
                # the optimizers, therefore if the aim is to
                # maximize a measure (e.g. +fc_corr) its cost
                # will be -fc_corr
                if term.startswith("-"):
                    self.obj_names.append(term.replace("-", "+"))
                elif term.startswith("+"):
                    self.obj_names.append(term.replace("+", "-"))
            if (self.model == 'rWW') & self.sim_group.do_fic & self.sim_group.fic_penalty:
                # TODO: consider creating separate problem classes for each model
                self.obj_names.append("+fic_penalty")
        else:
            self.obj_names = ["cost"]
        n_obj = len(self.obj_names)
        # initialize pymoo Problem
        super().__init__(
            n_var=self.ndim,
            n_obj=n_obj,
            n_ieq_constr=0,  # TODO: consider using this for enforcing FIC success
            xl=np.zeros(self.ndim, dtype=float),
            xu=np.ones(self.ndim, dtype=float),
        )

    def get_config(self, include_sim_group=True, include_N=False):
        """
        Get the problem configuration

        Parameters
        ----------
        include_sim_group : :obj:`bool`, optional
            whether to include the configuration of the
            associated :class:`cubnm.sim.SimGroup`
        include_N : :obj:`bool`, optional
            whether to include the current population size
            in the configuration

        Returns
        -------
        :obj:`dict`
            the configuration of the problem
        """
        config = {
            "params": self.params,
            "het_params": self.het_params,
            "maps": self.input_maps,
            "node_grouping": self.node_grouping,
            "emp_fc_tril": self.emp_fc_tril,
            "emp_fcd_tril": self.emp_fcd_tril,
        }
        if include_N:
            config["N"] = self.sim_group.N
        if include_sim_group:
            config.update(self.sim_group.get_config())
        return config

    def _get_Xt(self, X):
        """
        Transforms normalized parameters in range [0, 1] to
        the actual parameter ranges

        Parameters
        ----------
        X : :obj:`np.ndarray`
            the normalized parameters of current population. 
            Shape: (N, ndim)

        Returns
        -------
        :obj:`np.ndarray`
            the transformed parameters of current population. 
            Shape: (N, ndim)
        """
        return (X * (self.ub - self.lb)) + self.lb

    def _get_X(self, Xt):
        """
        Normalizes parameters to range [0, 1]

        Parameters
        ----------
        Xt : :obj:`np.ndarray`
            the parameters of current population. 
            Shape: (N, ndim)

        Returns
        -------
        :obj:`np.ndarray`
            the normalized parameters current population. 
            Shape: (N, ndim)
        """
        return (Xt - self.lb) / (self.ub - self.lb)

    def _set_sim_params(self, X):
        """
        Sets the global and regional parameters of the problem's 
        :class:`cubnm.sim.SimGroup` based on the
        problem free and fixed parameters and type of regional parameter
        heterogeneity (map-based, group-based or none).

        Parameters
        ----------
        X : :obj:`np.ndarray`
            the normalized parameters of current population in range [0, 1]. 
            Shape: (N, ndim)
        """
        # transform X from [0, 1] range to the actual
        # parameter range and label them
        Xt = pd.DataFrame(self._get_Xt(X), columns=self.free_params, dtype=float)
        # set fixed parameter lists
        # these are not going to vary across iterations
        # but must be specified here as in elsewhere N is unknown
        for param in self.fixed_params:
            if param in self.global_params:
                self.sim_group.param_lists[param] = np.repeat(
                    self.params[param], self.sim_group.N
                )
            else:
                self.sim_group.param_lists[param] = np.tile(
                    self.params[param], (self.sim_group.N, self.sim_group.nodes)
                )
        # first determine the global parameters and bias terms
        for param in self.free_params:
            if param in self.global_params:
                self.sim_group.param_lists[param] = Xt.loc[:, param].values
            elif param in self.regional_params:
                self.sim_group.param_lists[param] = np.tile(
                    Xt.loc[:, param].values[:, np.newaxis], self.sim_group.nodes
                )
        # then multiply the regional parameters by their map-based scalers
        if self.is_heterogeneous:
            for param in self.het_params:
                for sim_idx in range(Xt.shape[0]):
                    # not doing this vectorized for better code readability
                    # determine scaler map of current simulation-param
                    param_scalers = np.ones(self.sim_group.nodes)
                    for map_idx in range(self.maps.shape[0]):
                        scaler_name = f"{param}scale{map_idx}"
                        param_scalers += (
                            self.maps[map_idx, :] * Xt.iloc[sim_idx].loc[scaler_name]
                        )
                    # multiply it by the bias which is already put in param_lists
                    self.sim_group.param_lists[param][sim_idx, :] *= param_scalers
                    # fix/reject negatives
                    if self.sim_group.param_lists[param][sim_idx, :].min() < 0.001:
                        if self.reject_negative:
                            # TODO
                            raise NotImplementedError(
                                "Rejecting particles due to negative regional parameter is not implemented"
                            )
                        else:
                            self.sim_group.param_lists[param][sim_idx, :] -= (
                                self.sim_group.param_lists[param][sim_idx, :].min()
                                - 0.001
                            )
        # determine regional parameters that are variable based on groups
        if self.is_regional:
            for param in self.het_params:
                curr_param_maps = np.zeros((Xt.shape[0], self.sim_group.nodes))
                for group in self.node_groups:
                    param_name = f"{param}{group}"
                    curr_param_maps[:, self.memberships == group] = Xt.loc[
                        :, param_name
                    ].values[:, np.newaxis]
                self.sim_group.param_lists[param] = curr_param_maps

    def _evaluate(self, X, out, *args, **kwargs):
        scores = self.eval(X)
        if "scores" in kwargs:
            kwargs["scores"].append(scores)
        if self.multiobj:
            out["F"] = -scores.loc[:, self.sim_group.gof_terms].values
            if (self.model == 'rWW') & self.sim_group.do_fic & self.sim_group.fic_penalty:
                out["F"] = np.concatenate(
                    [out["F"], -scores.loc[:, ["-fic_penalty"]].values], axis=1
                )
        else:
            if (self.model == 'rWW') & self.sim_group.do_fic & self.sim_group.fic_penalty:
                out["F"] = (
                    -scores.loc[:, "-fic_penalty"] - scores.loc[:, "+gof"]
                ).values
            else:
                out["F"] = -scores.loc[:, "+gof"].values
            # out["G"] = ... # TODO: consider using this for enforcing FIC success

    def eval(self, X):
        """
        Runs the simulations based on normalized candidate free
        parameters `X` and evaluates their goodness of fit to
        the empirical FC and FCD of the problem.

        Parameters
        ----------
        X : :obj:`np.ndarray`
            the normalized parameters of current population in range [0, 1]. 
            Shape: (N, ndim)
        
        Returns
        -------
        :obj:`pd.DataFrame`
            The goodness of fit measures (columns) of each simulation (rows)
        """
        # set N to current iteration population size
        # which might be variable, e.g. from evaluating
        # CMAES initial guess to its next iterations
        self.sim_group.N = X.shape[0]
        self._set_sim_params(X)
        self.sim_group.run()
        return self.sim_group.score(self.emp_fc_tril, self.emp_fcd_tril)


class Optimizer(ABC):
    @abstractmethod
    def __init__(self, **kwargs):
        # defining __init__ as abstractmethod
        # for the docs to conform with subclasses
        """
        Base class for evolutionary optimizers
        """
        pass

    @abstractmethod
    def setup_problem(self, problem, **kwargs):
        pass

    @abstractmethod
    def optimize(self):
        pass

    def save(self, save_obj=False):
        """
        Saves the output of the optimizer, including history
        of particles, history of optima, the optimal point,
        and its simulation data. The output will be saved
        to `out_dir` of the problem's :class:`cubnm.sim.SimGroup`.
        If a directory with the same type of optimizer already
        exists, a new directory with a new index will be created.

        Parameters
        ---------
        save_obj : :obj:`bool`, optional
            saves the optimizer object which also includes the simulation
            data of all simulations and therefore can be large file.
            Warning: this file is very large.
        """
        # specify the directory
        run_idx = 0
        while True:
            dirname = (
                type(self).__name__.lower().replace("optimizer", "") + f"_run-{run_idx}"
            )
            optimizer_dir = os.path.join(self.problem.sim_group.out_dir, dirname)
            if not os.path.exists(optimizer_dir):
                break
            else:
                run_idx += 1
        os.makedirs(optimizer_dir, exist_ok=True)
        # save the optimizer object
        if save_obj:
            with open(os.path.join(optimizer_dir, "optimizer.pickle"), "wb") as f:
                pickle.dump(self, f)
        # save the optimizer history
        self.history.to_csv(os.path.join(optimizer_dir, "history.csv"))
        if self.problem.n_obj == 1:
            self.opt_history.to_csv(os.path.join(optimizer_dir, "opt_history.csv"))
            self.opt.to_csv(os.path.join(optimizer_dir, "opt.csv"))
        # save the configs of simulations and optimizer
        problem_config = self.problem.get_config(include_sim_group=True, include_N=True)
        ## problem config might include numpy arrays (for sc, sc_dist, etc.)
        ## which cannot be saved in the json file. If that's the case save
        ## these arrays as txt files within the optimizer directory and
        ## save their path instead of the arrays
        ## Also convert all relative paths to absolute paths
        for k, v in problem_config.items():
            if isinstance(v, np.ndarray):
                v_path = os.path.join(optimizer_dir, k+".txt")
                np.savetxt(v_path, v)
                problem_config[k] = v_path
            # convert relative to absoulte paths
            if (
                isinstance(problem_config[k], (str, os.PathLike)) and 
                os.path.exists(problem_config[k])
            ):
                problem_config[k] = os.path.abspath(problem_config[k])
        with open(os.path.join(optimizer_dir, "problem.json"), "w") as f:
            json.dump(
                problem_config,
                f,
                indent=4,
            )
        with open(os.path.join(optimizer_dir, "optimizer.json"), "w") as f:
            json.dump(self.get_config(), f, indent=4)
        # rerun optimum simulation(s) and save it
        print("Rerunning and saving the optimal simulation(s)")
        ## reuse the original problem used throughout the optimization
        ## but change its out_dir, N and parameters to the optimum
        self.problem.sim_group.out_dir = os.path.join(optimizer_dir, "opt_sim")
        os.makedirs(self.problem.sim_group.out_dir, exist_ok=True)
        res = self.algorithm.result()
        X = np.atleast_2d(res.X)
        Xt = pd.DataFrame(self.problem._get_Xt(X), columns=self.problem.free_params)
        opt_scores = self.problem.eval(X)
        pd.concat([Xt, opt_scores], axis=1).to_csv(
            os.path.join(self.problem.sim_group.out_dir, "res.csv")
        )
        ## save simulations data (including BOLD, FC and FCD, extended output, and param maps)
        self.problem.sim_group.save()
        # TODO: add option to save everything, including all the
        # SimGroup's of the different iterations

    def get_config(self):
        """
        Get the optimizer configuration

        Returns
        -------
        :obj:`dict`
            the configuration of the optimizer
        """
        # TODO: add optimizer-specific get_config funcitons
        return {"n_iter": self.n_iter, "popsize": self.popsize, "seed": self.seed}


class PymooOptimizer(Optimizer):
    def __init__(
        self, termination=None, n_iter=2, seed=0, print_history=True, 
        save_history_sim=False, **kwargs
    ):
        """
        Generic wrapper for `pymoo` optimizers.

        Parameters:
        ----------
        termination : :obj:`pymoo.termination.Termination`, optional
            The termination object that defines the stopping criteria for 
            the optimization process.
            If not provided, the termination criteria will be based on the 
            number of iterations (`n_iter`).
        n_iter : :obj:`int`, optional
            The maximum number of iterations for the optimization process.
            This parameter is only used if `termination` is not provided.
        seed : :obj:`int`, optional
            The seed value for the random number generator used by the optimizer.
        print_history : :obj:`bool`, optional
            Flag indicating whether to print the optimization history during the 
            optimization process.
        save_history_sim : :obj:`bool`, optional
            Flag indicating whether to save the simulation data of each iteration.
            Default is False to avoid consuming too much memory across iterations.
        **kwargs
            Additional keyword arguments that can be passed to the `pymoo` optimizer.        
        """
        # set optimizer seed
        self.seed = seed
        # set termination and n_iter (aka n_max_gen)
        self.termination = termination
        if self.termination:
            self.n_iter = self.termination.n_max_gen
        else:
            self.n_iter = n_iter
            self.termination = get_termination("n_iter", self.n_iter)
        self.print_history = print_history
        self.save_history_sim = save_history_sim

    def setup_problem(self, problem, pymoo_verbose=False, **kwargs):
        """
        Registers a :class:`cubnm.optimizer.BNMProblem` 
        with the optimizer, so that the optimizer can optimize
        its free parameters.

        Parameters
        ----------
        problem : :obj:`cubnm.optimizer.BNMProblem`
            The problem to be set up with the algorithm.
        pymoo_verbose : :obj:`bool`, optional
            Flag indicating whether to enable verbose output from pymoo. Default is False.
        **kwargs
            Additional keyword arguments to be passed to the algorithm setup method.
        """
        # setup the algorithm with the problem
        self.problem = problem
        if self.problem.n_obj > self.max_obj:
            raise ValueError(
                f"Maximum number of objectives for {type(self.algorithm)} = {self.max_obj} exceeds the number of problem objectives = {self.problem.n_obj}"
            )
        self.algorithm.setup(
            problem,
            termination=self.termination,
            seed=self.seed,
            verbose=pymoo_verbose,
            save_history=True,
            **kwargs,
        )

    def optimize(self):
        """
        Optimizes the associated :class:`cubnm.optimizer.BNMProblem`
        free parameters through an evolutionary optimization approach by
        running multiple generations of parallel simulations until the
        termination criteria is met or maximum number of iterations is reached.
        """
        self.history = []
        self.opt_history = []
        while self.algorithm.has_next():
            # ask the algorithm for the next solution to be evaluated
            pop = self.algorithm.ask()
            # evaluate the individuals using the algorithm's evaluator (necessary to count evaluations for termination)
            scores = []  # pass on an empty scores list to the evaluator to store the individual GOF measures
            self.algorithm.evaluator.eval(self.problem, pop, scores=scores)
            if not self.save_history_sim:
                # clear current simulation data
                # it has to be here before .tell
                self.problem.sim_group.clear()
            # returned the evaluated individuals which have been evaluated or even modified
            self.algorithm.tell(infills=pop)
            # TODO: do same more things, printing, logging, storing or even modifying the algorithm object
            X = np.array([p.x for p in self.algorithm.pop])
            Xt = pd.DataFrame(self.problem._get_Xt(X), columns=self.problem.free_params)
            Xt.index.name = "sim_idx"
            F = np.array([p.F for p in self.algorithm.pop])
            F = pd.DataFrame(F, columns=self.problem.obj_names)
            if self.problem.multiobj:
                # in multiobj optimizers simulations are reordered after scores are calculated
                # therefore don't concatenate scores to them
                # TODO: make sure this doesn't happen with the other single-objective optimizers
                # (other than CMAES)
                res = pd.concat([Xt, F], axis=1)
            else:
                res = pd.concat([Xt, F, scores[0]], axis=1)
            res["gen"] = self.algorithm.n_gen - 1
            if self.print_history:
                print(res.to_string())
            self.history.append(res)
            if self.problem.n_obj == 1:
                # a single optimum can be defined from each population
                self.opt_history.append(res.loc[res["cost"].argmin()])
        self.history = pd.concat(self.history, axis=0).reset_index(drop=False)
        if self.problem.n_obj == 1:
            # a single optimum can be defined
            self.opt_history = pd.DataFrame(self.opt_history).reset_index(drop=True)
            self.opt = self.history.loc[self.history["cost"].argmin()]


class CMAESOptimizer(PymooOptimizer):
    def __init__(
        self,
        popsize,
        x0=None,
        sigma=0.5,
        use_bound_penalty=False,
        algorithm_kws={},
        **kwargs,
    ):
        """
        Covariance Matrix Adaptation Evolution Strategy (CMA-ES) optimizer

        Parameters
        ----------
        popsize : :obj:`int`
            The population size for the optimizer
        x0 : array-like, optional
            The initial guess for the optimization.
            If None (default), the initial guess will be estimated based on
            20 random samples as the first generation
        sigma : :obj:`float`, optional
            The initial step size for the optimization
        use_bound_penalty : :obj:`bool`, optional
            Whether to use a bound penalty for the optimization
        algorithm_kws : :obj:`dict`, optional
            Additional keyword arguments for the CMAES algorithm
        **kwargs
            Additional keyword arguments

        Example
        -------
        Run a CMAES optimization for 10 iterations with 
        a population size of 20: ::

            from cubnm import datasets, optimize

            problem = optimize.BNMProblem(
                model = 'rWW',
                params = {
                    'G': (0.5, 2.5),
                    'wEE': (0.05, 0.75),
                    'wEI': 0.15,
                },
                emp_fc_tril = datasets.load_functional('FC', 'schaefer-100', exc_interhemispheric=True),
                emp_fcd_tril = datasets.load_functional('FCD', 'schaefer-100', exc_interhemispheric=True),
                duration = 60,
                TR = 1,
                sc_path = datasets.load_sc('strength', 'schaefer-100', return_path=True),
                states_ts = True
            )
            cmaes = optimize.CMAESOptimizer(popsize=20, n_iter=10, seed=1)
            cmaes.setup_problem(problem)
            cmaes.optimize()
            cmaes.save()
        """
        super().__init__(**kwargs)
        self.max_obj = 1
        self.use_bound_penalty = (
            use_bound_penalty  # this option will be set after problem is set up
        )
        self.popsize = popsize
        self.algorithm = CMAES(
            x0=x0,  # will estimate the initial guess based on 20 random samples
            sigma=sigma,
            popsize=popsize,  # from second generation
            **algorithm_kws,
        )

    def setup_problem(self, problem, **kwargs):
        """
        Extends :meth:`cubnm.optimizer.PymooOptimizer.setup_problem` to
        set up the optimizer with the problem and set the bound penalty
        option based on the optimizer's `use_bound_penalty` attribute.

        Parameters
        ----------
        problem : :obj:`cubnm.optimizer.BNMProblem`
            The problem to be set up with the algorithm.
        **kwargs
            Additional keyword arguments to be passed to 
            :meth:`cubnm.optimizer.PymooOptimizer.setup_problem`
        """
        super().setup_problem(problem, **kwargs)
        self.algorithm.options["bounds"] = [0, 1]
        if self.use_bound_penalty:
            self.algorithm.options["BoundaryHandler"] = cma.BoundPenalty
        else:
            self.algorithm.options["BoundaryHandler"] = cma.BoundTransform


class NSGA2Optimizer(PymooOptimizer):
    def __init__(self, popsize, algorithm_kws={}, **kwargs):
        """
        Non-dominated Sorting Genetic Algorithm II (NSGA-II) optimizer

        Parameters
        ----------
        popsize : int
            The population size for the optimizer
        algorithm_kws : dict, optional
            Additional keyword arguments for the NSGA2 algorithm
        kwargs : dict
            Additional keyword arguments for the base class
        """
        super().__init__(**kwargs)
        self.max_obj = 3
        self.popsize = popsize
        self.algorithm = NSGA2(pop_size=popsize, **algorithm_kws)


class BayesOptimizer(Optimizer):
    def __init__(self, popsize, n_iter, seed=0):
        """
        Bayesian optimizer

        Parameters
        ----------
        popsize : :obj:`int`
            The population size for the optimizer
        n_iter : :obj:`int`
            The number of iterations for the optimization process
        seed : :obj:`int`, optional
            The seed value for the random number generator used by the optimizer.
        """
        # does not initialize the optimizer yet because
        # the number of dimensions are not known yet
        # TODO: consider defining Problem before optimizer
        # and then passing it on to the optimizer
        self.max_obj = 1
        self.popsize = popsize
        self.n_iter = n_iter
        self.seed = seed
        self.save_history_sim=False # currently saving history of simulations is not implemented


    def setup_problem(self, problem, **kwargs):
        """
        Sets up the optimizer with the problem

        Parameters
        ----------
        problem : :obj:`cubnm.optimizer.BNMProblem`
            The problem to be set up with the algorithm.
        **kwargs
            Additional keyword arguments to be passed to :class:`skopt.Optimizer`
        """
        self.problem = problem
        self.algorithm = skopt.Optimizer(
            dimensions=[skopt.space.Real(0.0, 1.0)] * self.problem.ndim,
            random_state=self.seed,
            base_estimator="gp",
            acq_func="LCB",  # TODO: see if this is the best option
            **kwargs,
        )

    def optimize(self):
        """
        Optimizes the associated :class:`cubnm.optimizer.BNMProblem`
        free parameters through an evolutionary optimization approach by
        running multiple generations of parallel simulations until the
        termination criteria is met or maximum number of iterations is reached.
        """
        self.history = []
        self.opt_history = []
        for it in range(self.n_iter):
            # ask for next popsize suggestions
            X = np.array(self.algorithm.ask(n_points=self.popsize))
            # evaluate them
            out = {}
            self.problem._evaluate(X, out)
            # tell the results to the optimizer
            self.algorithm.tell(X.tolist(), out["F"].tolist())
            # print results
            res = pd.DataFrame(
                self.problem._get_Xt(X), columns=self.problem.free_params
            )
            res.index.name = "sim_idx"
            res["F"] = out["F"]
            print(res)
            res["gen"] = it + 1
            self.history.append(res)
            self.opt_history.append(res.loc[res["F"].argmin()])
        self.history = pd.concat(self.history, axis=0).reset_index(drop=False)
        self.opt_history = pd.DataFrame(self.opt_history).reset_index(drop=True)
        self.opt = self.opt_history.loc[self.opt_history["F"].argmin()]

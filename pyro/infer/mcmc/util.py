import torch
from collections import defaultdict

from pyro.distributions.util import log_sum_exp
from pyro.infer.util import is_validation_enabled
from pyro.poutine.indep_messenger import CondIndepStackFrame
from pyro.util import check_site_shape


class EnumTraceProbEvaluator(object):
    """
    Computes the log probability density of a trace that possibly contains
    discrete sample sites enumerated in parallel.

    :param model_trace: execution trace from the model.
    :param bool has_enumerable_sites: whether the trace contains any
        discrete enumerable sites.
    :param int max_iarange_nesting: Optional bound on max number of nested
        :func:`pyro.iarange` contexts.
    """
    def __init__(self,
                 model_trace,
                 has_enumerable_sites=False,
                 max_iarange_nesting=float("inf")):
        self.model_trace = model_trace
        self.has_enumerable_sites = has_enumerable_sites
        self.max_iarange_nesting = max_iarange_nesting
        self.log_probs = defaultdict(list)
        self._enum_dims = defaultdict(list)
        self._sorted_indep_stacks = []

    def _compute_log_prob_terms(self):
        """
        Computes the conditional probabilities for each of the sites
        in the model trace, and stores the result in `self.log_probs`.
        """
        if len(self.log_probs) > 0:
            return
        self.model_trace.compute_log_prob()
        default_cond_stack = (CondIndepStackFrame(name="default", dim=0, size=0, counter=None),)
        ordering = {}
        for name, site in self.model_trace.nodes.items():
            if site["type"] == "sample":
                if len(site["cond_indep_stack"]) == 0:
                    ordering[name] = frozenset(default_cond_stack)
                else:
                    ordering[name] = frozenset(default_cond_stack + site["cond_indep_stack"])

        # Collect log prob terms per independence context.
        for name, site in self.model_trace.nodes.items():
            if site["type"] == "sample":
                if is_validation_enabled():
                    check_site_shape(site, self.max_iarange_nesting)
                self.log_probs[ordering[name]].append(site["log_prob"])

        # Compute topological sorting for the indep stacks
        visited_nodes = set()
        enum_idx = self.max_iarange_nesting
        for ordinal in sorted(self.log_probs.keys()):
            self.log_probs[ordinal] = sum(self.log_probs[ordinal])
            marginal_dims = ordinal - visited_nodes
            enum_dims = list(range(-self.log_probs[ordinal].dim(), -enum_idx))
            enum_idx = max(self.log_probs[ordinal].dim(), enum_idx)
            self._sorted_indep_stacks += sorted(list(marginal_dims))
            self._enum_dims[ordinal] = enum_dims
            visited_nodes = visited_nodes.union(ordinal)


    def log_prob(self):
        """
        Returns the log pdf of `model_trace` by appropriate handling
        of the enumerated log prob factors.

        :return: log pdf of the trace.
        """
        if not self.has_enumerable_sites:
            return self.model_trace.log_prob_sum()
        if self.max_iarange_nesting == float("inf"):
            raise ValueError("Finite value required for `max_iarange_nesting` when model "
                             "has discrete (enumerable) sites.")
        self._compute_log_prob_terms()

        # Reduce log_prob dimensions starting from the leaf indep contexts.
        # Each ordinal is visited once and the ordinal's log prob after
        # reduction is aggregated into `log_prob`.
        log_prob = torch.tensor(0.)
        to_visit = set(self.log_probs.keys())
        for frame in reversed(self._sorted_indep_stacks):
            visited = set()
            for ordinal in to_visit:
                if frame in ordinal:
                    # Reduce the log prob terms for each node:
                    # - taking log_sum_exp of factors in enum dims (i.e.
                    # adding up the probability terms).
                    # - summing up the dims within `max_iarange_nesting`.
                    # (i.e. multiplying probs within independent batches).
                    log_prob = log_prob + self.log_probs[ordinal]
                    for enum_dim in self._enum_dims[ordinal]:
                        log_prob = log_sum_exp(log_prob, dim=enum_dim, keepdim=True)
                    log_prob = log_prob.sum(dim=frame.dim, keepdim=True)
                    visited.add(ordinal)
            to_visit = to_visit - visited
        return log_prob.sum()

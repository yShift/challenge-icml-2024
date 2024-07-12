import networkx as nx
import numpy as np
import torch
import torch_geometric

from modules.transforms.liftings.graph2hypergraph.base import Graph2HypergraphLifting


class HypergraphNodeCentralityLifting(Graph2HypergraphLifting):
    r"""Lifts graphs to hypergraph domain using node centrality.

    This lifting creates hyperedges based on central, i.e. highly influential, nodes in the network. Mapping a connection between individual nodes to specific nodes in the network architecture that have a specific and potentially competing influence on them is a very convenient scenario to be modelled via hyperedges. Using shortest path distance to identify the most influential nodes on any given node even allows for placing weights on the hyperedge connection to individual, connected nodes (i.e. the inverse shortest path distance to the corresponding most influential node that the hyperedge represents). To define and identify influential nodes in the network, we refer to the variant of the Eigenvector Centrality with an additional jump probability (i.e. PageRank)

    Parameters
    ----------
    network_type : str
        Network type may be weighted or unweighted. Default is "weighted".
    alpha: float
        jump probability, called dampening factor, which decides whether to continue following the transition matrix or teleport to random positions, default=0.85.
    th_percentile: float
        Fraction of most influential nodes in the network to consider, default=0.05.
    n_most_influential: integer
        Number of most influential nodes to assign a node to. default=2.
    do_weight_hyperedge_influence: bool
        add a weight to the hyperedge connections per node based on the inverse spath distance to influential node. default=False.
    do_hyperedge_node_assignment_feature_lifting_passthrough: bool
        assign features of most influential nodes to corresponding hyperedges and pass through feature lifting. default=False.
    max_iter: integer
        Maximum number of iterations in power method eigenvalue solver.
    tol: float
        Error tolerance used to check convergence in power method solver. The iteration will stop after a tolerance of len(G) * tol is reached.

    **kwargs : optional
        Additional arguments for the class.
    """

    def __init__(
        self,
        network_type: str = "weighted",
        alpha: float = 0.85,
        th_percentile: float = 0.05,
        n_most_influential: float = 2,
        do_weight_hyperedge_influence: bool = False,
        do_hyperedge_node_assignment_feature_lifting_passthrough: bool = False,
        max_iter: int = 100,
        tol: float = 1e-06,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self.network_type = network_type
        self.alpha = alpha
        self.max_iter = max_iter
        self.tol = tol
        self.th_percentile = th_percentile
        self.n_most_influential = n_most_influential
        self.do_weight_hyperedge_influence = do_weight_hyperedge_influence
        self.do_hyperedge_node_assignment_feature_lifting_passthrough = (
            do_hyperedge_node_assignment_feature_lifting_passthrough
        )

    def lift_topology(self, data: torch_geometric.data.Data) -> dict:
        r"""Lifts the topology of a graph to hypergraph domain using node centrality.

        Parameters
        ----------
        data : torch_geometric.data.Data
            The input data to be lifted.

        Returns
        -------
        dict
            The lifted topology.
        """

        edge_list = data.edge_index.t().numpy()

        # for unweighted graphs or higher-dimensional edge or node features revert to unweighted network structure
        if (
            data.edge_attr is None
            or self.network_type == "unweighted"
            or data.edge_attr.shape[1] > 1
        ):
            edge_attr = np.ones(shape=(len(edge_list), 1))
        elif isinstance(data.edge_attr, torch.Tensor):
            edge_attr = data.edge_attr.numpy()
        else:
            edge_attr = data.edge_attr

        if data.x is None or self.network_type == "unweighted" or data.x.shape[1] > 1:
            node_attr = np.ones(shape=(data.num_nodes, 1))
        elif isinstance(data.x, torch.Tensor):
            node_attr = data.x.numpy()
        else:
            node_attr = data.x

        # create directed networkx graph from pyg data
        G = nx.Graph()
        for v in range(len(node_attr)):
            G.add_node(v)
            G.nodes[v]["w"] = node_attr[v][0]

        for e in range(len(edge_list)):
            v1 = edge_list[e][0]
            v2 = edge_list[e][1]
            G.add_edge(v1, v2, w=edge_attr[e][0])

        assert self.n_most_influential >= 1

        # estimate distance between all nodes
        if self.network_type == "unweighted":
            sp = dict(nx.all_pairs_shortest_path_length(G))
        elif self.network_type == "weighted":
            sp = dict(nx.all_pairs_dijkstra_path_length(G))
        else:
            raise NotImplementedError(
                f"network type {self.network_type} not implemented"
            )

        # estimate node centrality for all nodes
        pr = nx.pagerank(
            G, alpha=self.alpha, max_iter=self.max_iter, tol=self.tol, weight="w"
        )

        # estimate fraction of most influential nodes in the network to consider, i.e. the hyperedges
        th_cutoff = np.quantile(list(pr.values()), (1 - self.th_percentile))
        nodes_most_influential = [n for n, v in pr.items() if v >= th_cutoff]
        num_hyperedges = len(nodes_most_influential)
        hyperedge_map = {v: e for e, v in enumerate(nodes_most_influential)}

        incidence_hyperedges = torch.zeros(data.num_nodes, num_hyperedges)

        # assign each node to the hyeredges corresponding to the top "n_most_influential" most influential nodes
        for v in list(G.nodes()):
            if v in nodes_most_influential:
                incidence_hyperedges[v, hyperedge_map[v]] = 1
            else:
                sp_v_influencial = {
                    k: v for k, v in sp[v].items() if k in nodes_most_influential
                }
                v_influencial = [
                    (k, v)
                    for i, (k, v) in enumerate(sp_v_influencial.items())
                    if i < self.n_most_influential
                ]
                for k_infl, v_infl in v_influencial:
                    w = 1
                    if self.do_weight_hyperedge_influence:
                        w = max(1 / v_infl, 0.0001)
                    incidence_hyperedges[v, hyperedge_map[k_infl]] = w

        incidence_hyperedges = incidence_hyperedges.to_sparse_coo()
        lifted_data = {
            "incidence_hyperedges": incidence_hyperedges,
            "num_hyperedges": num_hyperedges,
            "x_0": data.x,
        }

        if self.do_hyperedge_node_assignment_feature_lifting_passthrough:
            # assign features of most influential nodes to corresponding hyperedges and pass through feature lifting.
            lifted_data["x_hyperedges"] = data.x[nodes_most_influential]

        return lifted_data

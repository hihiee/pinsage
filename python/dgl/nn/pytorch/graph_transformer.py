"""Torch modules for graph transformers."""
import torch as th
import torch.nn as nn
import torch.nn.functional as F
import dgl

__all__ = ["DegreeEncoder", "BiasedMultiheadAttention"]

class DegreeEncoder(nn.Module):
    r"""Degree Encoder, as introduced in
    `Do Transformers Really Perform Bad for Graph Representation?
    <https://proceedings.neurips.cc/paper/2021/file/f1c1592588411002af340cbaedd6fc33-Paper.pdf>`__
    This module is a learnable degree embedding module.

    Parameters
    ----------
    max_degree : int
        Upper bound of degrees to be encoded.
        Each degree will be clamped into the range [0, ``max_degree``].
    embedding_dim : int
        Output dimension of embedding vectors.
    direction : str, optional
        Degrees of which direction to be encoded,
        selected from ``in``, ``out`` and ``both``.
        ``both`` encodes degrees from both directions
        and output the addition of them.
        Default : ``both``.

    Example
    -------
    >>> import dgl
    >>> from dgl.nn import DegreeEncoder

    >>> g = dgl.graph(([0,0,0,1,1,2,3,3], [1,2,3,0,3,0,0,1]))
    >>> degree_encoder = DegreeEncoder(5, 16)
    >>> degree_embedding = degree_encoder(g)
    """

    def __init__(self, max_degree, embedding_dim, direction="both"):
        super(DegreeEncoder, self).__init__()
        self.direction = direction
        if direction == "both":
            self.degree_encoder_1 = nn.Embedding(
                max_degree + 1, embedding_dim, padding_idx=0
            )
            self.degree_encoder_2 = nn.Embedding(
                max_degree + 1, embedding_dim, padding_idx=0
            )
        else:
            self.degree_encoder = nn.Embedding(
                max_degree + 1, embedding_dim, padding_idx=0
            )
        self.max_degree = max_degree

    def forward(self, g):
        """
        Parameters
        ----------
        g : DGLGraph
            A DGLGraph to be encoded. If it is a heterogeneous one,
            it will be transformed into a homogeneous one first.

        Returns
        -------
        Tensor
            Return degree embedding vectors of shape :math:`(N, embedding_dim)`,
            where :math:`N` is th number of nodes in the input graph.
        """
        if len(g.ntypes) > 1 or len(g.etypes) > 1:
            g = dgl.to_homogeneous(g)
        in_degree = th.clamp(g.in_degrees(), min=0, max=self.max_degree)
        out_degree = th.clamp(g.out_degrees(), min=0, max=self.max_degree)

        if self.direction == "in":
            degree_embedding = self.degree_encoder(in_degree)
        elif self.direction == "out":
            degree_embedding = self.degree_encoder(out_degree)
        elif self.direction == "both":
            degree_embedding = (self.degree_encoder_1(in_degree)
                                + self.degree_encoder_2(out_degree))
        else:
            raise ValueError(
                f'Supported direction options: "in", "out" and "both", '
                f'but got {self.direction}'
            )

        return degree_embedding


class BiasedMultiheadAttention(nn.Module):
    r"""Dense Multi-Head Attention Module with Graph Attention Bias.

    Compute attention between nodes with attention bias obtained from graph
    structures, as introduced in `Do Transformers Really Perform Bad for
    Graph Representation? <https://arxiv.org/pdf/2106.05234>`__

    .. math::

        \text{Attn}=\text{softmax}(\dfrac{QK^T}{\sqrt{d}} \circ b)

    :math:`Q` and :math:`K` are feature representation of nodes. :math:`d`
    is the corresponding :attr:`feat_size`. :math:`b` is attention bias, which
    can be additive or multiplicative according to the operator :math:`\circ`.

    Parameters
    ----------
    feat_size : int
        Feature size.
    num_heads : int
        Number of attention heads, by which attr:`feat_size` is divisible.
    bias : bool, optional
        If True, it uses bias for linear projection. Default: True.
    attn_bias_type : str, optional
        The type of attention bias used for modifying attention. Selected from
        'add' or 'mul'. Default: 'add'.

        * 'add' is for additive attention bias.
        * 'mul' is for multiplicative attention bias.
    attn_drop : float, optional
        Dropout probability on attention weights. Defalt: 0.1.

    Examples
    --------
    >>> import torch as th
    >>> from dgl.nn import BiasedMultiheadAttention

    >>> ndata = th.rand(16, 100, 512)
    >>> bias = th.rand(16, 100, 100, 8)
    >>> net = BiasedMultiheadAttention(feat_size=512, num_heads=8)
    >>> out = net(ndata, bias)
    """

    def __init__(self, feat_size, num_heads, bias=True, attn_bias_type="add", attn_drop=0.1):
        super().__init__()
        self.feat_size = feat_size
        self.num_heads = num_heads
        self.head_dim = feat_size // num_heads
        assert (
            self.head_dim * num_heads == feat_size
        ), "feat_size must be divisible by num_heads"
        self.scaling = self.head_dim**-0.5
        self.attn_bias_type = attn_bias_type

        self.q_proj = nn.Linear(feat_size, feat_size, bias=bias)
        self.k_proj = nn.Linear(feat_size, feat_size, bias=bias)
        self.v_proj = nn.Linear(feat_size, feat_size, bias=bias)
        self.out_proj = nn.Linear(feat_size, feat_size, bias=bias)

        self.dropout = nn.Dropout(p=attn_drop)

        self.reset_parameters()

    def reset_parameters(self):
        """Reset parameters of projection matrices, the same settings as that in Graphormer.
        """
        nn.init.xavier_uniform_(self.q_proj.weight, gain=2**-0.5)
        nn.init.xavier_uniform_(self.k_proj.weight, gain=2**-0.5)
        nn.init.xavier_uniform_(self.v_proj.weight, gain=2**-0.5)

        nn.init.xavier_uniform_(self.out_proj.weight)
        if self.out_proj.bias is not None:
            nn.init.constant_(self.out_proj.bias, 0.0)

    def forward(self, ndata, attn_bias=None, attn_mask=None):
        """Forward computation.

        Parameters
        ----------
        ndata : torch.Tensor
            A 3D input tensor. Shape: (batch_size, N, :attr:`feat_size`), where
            N is the maximum number of nodes.
        attn_bias : torch.Tensor, optional
            The attention bias used for attention modification. Shape:
            (batch_size, N, N, :attr:`num_heads`).
        attn_mask : torch.Tensor, optional
            The attention mask used for avoiding computation on invalid positions, where
            invalid positions are indicated by non-zero values. Shape: (batch_size, N, N).

        Returns
        -------
        y : torch.Tensor
            The output tensor. Shape: (batch_size, N, :attr:`feat_size`)
        """
        q_h = self.q_proj(ndata).transpose(0, 1)
        k_h = self.k_proj(ndata).transpose(0, 1)
        v_h = self.v_proj(ndata).transpose(0, 1)
        bsz, N, _ = ndata.shape
        q_h = q_h.reshape(N, bsz * self.num_heads, self.head_dim).transpose(0, 1) / self.scaling
        k_h = k_h.reshape(N, bsz * self.num_heads, self.head_dim).permute(1, 2, 0)
        v_h = v_h.reshape(N, bsz * self.num_heads, self.head_dim).transpose(0, 1)

        attn_weights = (
            th.bmm(q_h, k_h)
            .transpose(0, 2)
            .reshape(N, N, bsz, self.num_heads)
            .transpose(0, 2)
        )

        if attn_bias is not None:
            if self.attn_bias_type == "add":
                attn_weights += attn_bias
            else:
                attn_weights *= attn_bias

        if attn_mask is not None:
            attn_weights[attn_mask.to(th.bool)] = float("-inf")

        attn_weights = F.softmax(
            attn_weights.transpose(0, 2)
            .reshape(N, N, bsz * self.num_heads)
            .transpose(0, 2),
            dim=2,
        )

        attn_weights = self.dropout(attn_weights)

        attn = th.bmm(attn_weights, v_h).transpose(0, 1)

        attn = self.out_proj(attn.reshape(N, bsz, self.feat_size).transpose(0, 1))

        return attn

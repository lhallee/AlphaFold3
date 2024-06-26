"""
Code adapted from original paper
https://www.nature.com/articles/s41586-024-07487-w
lucidrains
https://github.com/lucidrains/alphafold3-pytorch
ZiyaoLi
https://github.com/ZiyaoLi/AlphaFold3
HZ3519
https://github.com/HZ3519/alphafold3
and openfold
https://github.com/aqlaboratory/openfold

Not optimized for speed, working on correctness for now.
Once all 31 algorithms are well implemented and tensors shapes are commented, it will be easy to build the final model.
The eventual goal is fast, understandable, and Huggingface integrated.
If some tensor shapes aren't commented, an algorithm is not done or wrong, or you see an improvement, please contribute!
"""

import torch
import torch.nn as nn
import torch.nn.functional as F

from functools import partial
from einops import rearrange, repeat
from einops.layers.torch import Rearrange

from attention import Attention
from configuration_alphafold import AlphaFoldConfig as config

# n_token - number of tokens
# n_templ - number of templates
# n_msa - number of msa rows
# n_perm - number of atom permuations
# n_chains - number of chains
# n_bonds - number of bonds
# indices i, j, k reference the token dimension
# indices l, m reference the flat atom dimension
# indices s, t reference the sequence dimension
# h references the attention head dim
# b - minibatch dim

# residue_index (n_token) - Residue number in the token's original input chain.
# token_index (n_token) - Token number. Increases monotonically; does not restart at 1 for new chains.
# asym_id (n_token) - Unique integer for each distinct chain.
# entity_id (n_token) - Unique integer for each distinct sequence.
# sym_id (n_token) - Unique integer within chains of this sequence. E.g. if chains A, B and C share a sequence but D does not, their sym_ids would be (0, 1, 2, 0).
# restype (n_token, 32) - One-hot encoding of the sequence. 32 possible values: 20 amino acids + unknown, 4 RNA nucleotides + unknown, 4 DNA nucleotides + unknown, and gap. Ligands represented as "unknown amino acid".
# is_protein / rna / dna / ligand (n_token) - 4 masks indicating the molecule type of a particular token.
# ref_pos (n_atom, 3) - Atom positions in the reference conformer, with a random rotation and translation applied. Atom positions are given in Å.
# ref_mask (n_atom) - Mask indicating which atom slots are used in the reference conformer.
# ref_element (n_atom, 128) - One-hot encoding of the element atomic number for each atom in the reference conformer, up to atomic number 128.
# ref_charge (n_atom) - Charge for each atom in the reference conformer.
# ref_atom_name_chars (n_atom, 4, 64) - One-hot encoding of the unique atom names in the reference conformer. Each character is encoded as ord(c) - 32, and names are padded to length 4.
# ref_space_uid (n_atom) - Numerical encoding of the chain id and residue index associated with this reference conformer. Each (chain id, residue index) tuple is assigned an integer on first appearance.
# msa (n_msa, n_token, 32) - One-hot encoding of the processed MSA, using the same classes as restype.
# has_deletion (n_msa, n_token) - Binary feature indicating if there is a deletion to the left of each position in the MSA.
# deletion_value (n_msa, n_token) - Raw deletion counts (the number of deletions to the left of each MSA position) are transformed to [0, 1] using 2 π arctan d 3 .
# profile (n_token, 32) - Distribution across restypes in the main MSA. Computed before MSA processing (subsection 2.3).
# deletion_mean (n_token) - Mean number of deletions at each position in the main MSA. Computed before MSA processing (subsection 2.3).
# template_restype (n_templ, n_token) - One-hot encoding of the template sequence, see restype.
# template_pseudo_beta_mask (n_templ, n_token) - Mask indicating if the Cβ (Cα for glycine) has coordinates for the template at this residue.
# template_backbone_frame_mask (n_templ, n_token) - Mask indicating if coordinates exist for all atoms required to compute the backbone frame (used in the template_unit_vector feature).
# template_distogram (n_templ, n_token, n_token, 39) - A one-hot pairwise feature indicating the distance between Cβ atoms (Cα for glycine). Pairwise distances are discretized into 38 bins of equal width between 3.25 Å and 50.75 Å; one more bin contains any larger distances.
# template_unit_vector (n_templ, n_token, n_token, 3) - The unit vector of the displacement of the Cα atom of all residues within the local frame of each residue. Local frames are computed as in [1].
# token_bonds (n_token, n_token) - A 2D matrix indicating if there is a bond between any atom in token i and token j, restricted to just polymer-ligand and ligand-ligand bonds and bonds less than 2.4 Å during training.

LinearNoBias = partial(nn.Linear, bias = False)
Linear = partial(nn.Linear, bias=True)

def exists(v):
    return v is not None

def default(v, d):
    return v if exists(v) else d


# Algorithm 1
class MainInferenceLoop(nn.Module):
    def __init__(self, config):
        super().__init__()
        self.input_embedder = InputFeatureEmbedder(config)
        self.pos_encoding = RelativePositionEncoding(config)
        self.template_embedder = TemplateEmbedder(config)
        self.msa_module = MSAModule(config)

    def forward(self, f):
        s_inputs = self.input_embedder(f) # (B, n_token, dim_single)
        pass


# Algorithm 2
class InputFeatureEmbedder(nn.Module):
    def __init__(self, config):
        super().__init__()
        self.encoder = AtomAttentionEncoder(config)

    def forward(self, features, r_l, s_trunk, z_ij):
        token_rep = self.encoder(features, r_l, s_trunk, z_ij)
        s = torch.concat(
            token_rep, # (b, n_token, c_token)
            features['restype'], # (b, n_token, 32)
            features['profile'], # (b, n_token, 32)
            features['deletion_mean'].unsqueeze(-1) # (b, n_token, 1)
        ) # (b, n_token, c_token+65)
        return s


# Algorithm 3
class RelativePositionEncoding(nn.Module):
    def __init__(self, config):
        super().__init__()
        self.r_max = config.r_max
        self.s_max = config.s_max

    def forward(self, features, dtype = torch.float):
        asym_id = features["asym_id"] # (n_token)
        sym_id = features["sym_id"] # (n_token)
        entity_id = features["entity_id"] # (n_token)
        token_idx = features["token_index"] # (n_token)
        res_idx = features["residue_index"] # (n_token)

        def _pair_same(x):
            return x[..., :, None] == x[..., None, :]

        def _pair_diff(x):
            return x[..., :, None] - x[..., None, :]

        b_same_chain = _pair_same(asym_id)
        b_same_res = _pair_same(res_idx) & b_same_chain # same res must be same chain
        b_same_entity = _pair_same(entity_id)

        d_res = torch.where(
            b_same_chain,
            torch.clip(_pair_diff(res_idx) + self.r_max, min=0, max=2*self.r_max),
            2*self.r_max+1,
        )
        rel_pos = one_hot(d_res, 2*self.r_max+2)

        d_token = torch.where(
            b_same_res,
            torch.clip(_pair_diff(token_idx) + self.r_max, min=0, max=2*self.r_max),
            2*self.r_max+1,
        )
        rel_token = one_hot(d_token, 2*self.r_max+2)

        d_chain = torch.where(
            ~b_same_chain,
            torch.clip(_pair_diff(sym_id) + self.s_max, min=0, max=2*self.s_max),
            2*self.s_max+1
        )
        rel_chain = one_hot(d_chain, 2*self.s_max+2)

        ret = torch.cat(
            [rel_pos, rel_token, b_same_entity.float()[..., None], rel_chain], dim=-1
        ).to(dtype)
        return ret


# Algorithm 4
def one_hot(x, bins):
    p = torch.zeros(len(bins))
    b = torch.argmin(torch.abs(x - bins))
    p[b] = 1
    return p  


# Algorithm 5
class AtomToTokenPooler(nn.Module):
    def __init__(self, dim, dim_out=None, atoms_per_window=27):
        super().__init__()
        dim_out = default(dim_out, dim)

        self.proj = nn.Sequential(
            LinearNoBias(dim, dim_out),
            nn.ReLU()
        )

        self.atoms_per_window = atoms_per_window

    def forward(self, atom_feats, atom_mask):
        # Masked mean pool the atom features for each residue, for the token transformer
        w = self.atoms_per_window

        atom_feats = self.proj(atom_feats)

        windowed_atom_feats = rearrange(atom_feats, 'b (n w) da -> b n w da', w=w)
        windowed_atom_mask = rearrange(atom_mask, 'b (n w) -> b n w', w=w)

        assert windowed_atom_mask.any(dim=-1).all(), 'Atom mask must contain one valid atom for each window'

        windowed_atom_feats = windowed_atom_feats.masked_fill(~windowed_atom_mask.unsqueeze(-1), 0.)

        num = windowed_atom_feats.sum(dim=-2)
        den = windowed_atom_mask.float().sum(dim=-1, keepdim=True)

        tokens = num / den
        return tokens
    

class AtomAttentionEncoder(nn.Module):
    def __init__(self, config):
        super().__init__()
        c_atom = config.c_atom
        c_atompair = config.c_atompair
        c_token = config.c_token
        self.linear_c = LinearNoBias(389, c_atom) # 3+1+1+128+256
        self.linear_pair1 = LinearNoBias(3, c_atompair)
        self.linear_pair2 = LinearNoBias(1, c_atompair)
        self.linear_pair3 = LinearNoBias(1, c_atompair)
        self.linear_c_trunk = LinearNoBias(c_token, c_atom)
        self.layer_norm_s_trunk = nn.LayerNorm(c_token)
        self.linear_p_trunk = LinearNoBias(c_token, c_atompair)
        self.layer_norm_z = nn.LayerNorm(c_token)
        self.linear_q_noisy = LinearNoBias(3, c_atom)
        self.linear_p_c1 = LinearNoBias(c_atom, c_atompair)
        self.linear_p_c2 = LinearNoBias(c_atom, c_atompair)
        self.mlp_p = nn.Sequential(
            LinearNoBias(c_atompair, c_atompair),
            nn.ReLU(),
            LinearNoBias(c_atompair, c_atompair),
            nn.ReLU(),
            LinearNoBias(c_atompair, c_atompair),
            nn.ReLU(),
            LinearNoBias(c_atompair, c_atompair)
        )
        self.atom_transformer = AtomTransformer(n_blocks=3, n_heads=4)
        self.atom_pooler = AtomToTokenPooler(c_atom, c_token)

    def forward(self, features, r_l, s_trunk_i, z_ij):
        # a_i : token_rep, q_l : noised_rep, c_l : single_rep, p_lm : pair_rep

        # save meta data
        ref_pos = features['ref_pos'] # (b, n_atom, 3)
        ref_space_uid = features['ref_space_uid'] # (b, n_atom)
        tok_idx = features['token_index'] # (b, n_token)

        # Create the atom single conditioning: Embed per-atom meta data
        single_rep = self.linear_c(torch.cat([
                ref_pos,
                features['ref_charge'].view(-1, 1), # (b, n_atom, 1)
                features['ref_mask'].view(-1, 1), # (b, n_atom, 1)
                features['ref_element'], # (b, n_atom, 128)
                features['ref_atom_name_chars'].view(-1, 4 * 64) # (b, n_atom, 256)
                # (b, n_atom, 389)
            ], dim=-1)) # (b, n_atom, c_atom)

        # Embed offsets between atom reference positions
        d_lm = ref_pos[:, None, :] - ref_pos[:, :, None] # (b, n_atom, n_atom, 3)
        ref_space_uid = ref_space_uid.unsqueeze(-1)
        v_lm = (ref_space_uid[:, None, :] == ref_space_uid[:, :, None]).float() # (b, n_atom, n_atom, 1)
        pair_rep = self.linear_pair1(d_lm) * v_lm # (b, n_atom, n_atom, c_atompair)

        # Embed pairwise inverse squared distances, and the valid mask
        pair_rep += self.linear_pair2(1 / (1 + torch.sum(d_lm**2, dim=-1, keepdim=True))) * v_lm # (b, n_atom, n_atom, c_atompair)
        pair_rep += self.linear_pair3(v_lm) # (b, n_atom, n_atom, c_atompair)

        # Initialize the atom single representation as the single conditioning
        noised_rep = single_rep.clone()

        # If provided, add trunk embeddings and noisy positions
        if r_l is not None:
            # Broadcast the single and pair embedding from the trunk
            single_rep += self.linear_c_trunk(self.layer_norm_s_trunk(s_trunk_i[tok_idx])) # (b, n_token, c_atom)
            pair_rep += self.linear_p_trunk(self.layer_norm_z(z_ij[tok_idx[:, None], tok_idx[None, :]])) # (b, n_token, n_token, c_atom)

            # Add the noisy positions
            noised_rep += self.linear_q_noisy(r_l) # (b, n_token, c_atom)

        # Add the combined single conditioning to the pair representation
        pair_rep += self.linear_p_c1(torch.relu(single_rep[:, None, :])) + self.linear_p_c2(torch.relu(single_rep[:, :, None])) # (b, n_atom, n_atom, c_atompair)

        # Run a small MLP on the pair activations
        pair_rep = self.mlp_p(pair_rep) # (b, n_atom, n_atom, c_atompair)

        # Cross attention transformer - noised_repr, single_repr, pairwise_repr
        noised_rep = self.atom_transformer(noised_rep, single_rep, pair_rep)

        # Aggregate per-atom representation to per-token representation
        token_rep = self.atom_pooler(noised_rep) # (b, n_token, c_token)

        noised_skip, single_skip, pair_skip = noised_rep, single_rep, pair_rep

        return token_rep, noised_skip, single_skip, pair_skip


# Algorithm 6
class AtomAttentionDecoder(nn.Module):
    def __init__(self, config, n_blocks=3, n_heads=4):
        super().__init__()
        self.linear_q = LinearNoBias(config.c_token, config.c_atom)
        self.atom_transformer = AtomTransformer(n_blocks, n_heads)
        self.linear_r_update = nn.Sequential(
            nn.LayerNorm(config.c_atom),
            LinearNoBias(config.c_atom, 3)
        )

    def forward(self, a_i, q_skip_l, c_skip_l, p_skip_lm, tok_idx):
        # Broadcast per-token activations to per-atom activations and add the skip connection
        q_l = self.linear_q(a_i[tok_idx]) + q_skip_l

        # Cross attention transformer
        q_l = self.atom_transformer(q_l, c_skip_l, p_skip_lm)

        # Map to positions update
        r_update_l = self.linear_r_update(q_l)

        return r_update_l


# Algorithm 7
class AtomTransformer(nn.Module):
    def __init__(self, n_blocks, n_heads, n_queries=32, n_keys=128, subset_centres=None):
        super().__init__()
        self.n_queries = n_queries
        self.n_keys = n_keys
        if subset_centres is None:
            subset_centres = [15.5, 47.5, 79.5]  # not complete, not sure where to find
        self.subset_centres = torch.tensor(subset_centres, dtype=torch.float32)
        self.diffusion_transformer = DiffusionTransformer(n_blocks, n_heads)

    def forward(self, q_l, c_l, p_lm):
        # Sequence-local atom attention is equivalent to self attention within rectangular blocks along the diagonal
        l = torch.arange(q_l.shape[0], dtype=torch.float32, device=q_l.device)
        m = l.view(-1, 1)
        c = self.subset_centres.view(1, -1, 1).to(q_l.device)

        mask_queries = (l.view(-1, 1) - c).abs() < self.n_queries / 2
        mask_keys = (m - c).abs() < self.n_keys / 2
        mask = mask_queries.unsqueeze(-1) & mask_keys.unsqueeze(0)

        beta_lm = torch.where(mask.any(dim=1, keepdim=True), torch.tensor(0.0, device=q_l.device),
                              torch.tensor(-1e10, device=q_l.device))

        # Apply the DiffusionTransformer
        q_l = self.diffusion_transformer(q_l, c_l, p_lm, beta_lm)

        return q_l


# Algorithm 8
class MSAModule(nn.Module):
    def __init__(self, config, pairwise_block_kwargs=None):
        super().__init__()
        if pairwise_block_kwargs is None:
            pairwise_block_kwargs = {}

        self.msa_init_proj = LinearNoBias(config.dim_msa_input, config.dim_msa) if config.dim_msa_input is not None else nn.Identity()
        self.single_to_msa_feats = LinearNoBias(config.dim_single, config.dim_msa)

        self.layers = nn.ModuleList([])
        for _ in range(config.msa_depth):
            msa_pre_ln = partial(nn.LayerNorm, normalized_shape=config.dim_msa)
            outer_product_mean = OuterProductMean(config)
            msa_pair_weighted_avg = MSAPairWeightedAveraging(config)
            msa_transition = Transition(config.dim_msa)
            pairwise_block = PairwiseBlock(dim_pair=config.dim_pair, **pairwise_block_kwargs)
            self.layers.append(nn.ModuleList([
                outer_product_mean,
                msa_pair_weighted_avg,
                msa_pre_ln(msa_transition),
                pairwise_block
            ]))

    def forward(self, single_repr, pairwise_repr, msa, mask=None, msa_mask=None):
        msa = self.msa_init_proj(msa)

        single_msa_feats = self.single_to_msa_feats(single_repr)
        msa = rearrange(single_msa_feats, 'b n d -> b 1 n d') + msa

        for outer_product_mean, msa_pair_weighted_avg, msa_transition, pairwise_block in self.layers:
            # Communication between msa and pairwise rep
            pairwise_repr = outer_product_mean(msa, mask=mask, msa_mask=msa_mask) + pairwise_repr
            msa = msa_pair_weighted_avg(msa=msa, pairwise_repr=pairwise_repr, mask=mask) + msa
            msa = msa_transition(msa) + msa

            # Pairwise block
            pairwise_repr = pairwise_block(pairwise_repr=pairwise_repr, mask=mask)

        return pairwise_repr


# Algorithm 9
class OuterProductMean(nn.Module):
    def __init__(self, config):
        super().__init__()
        c = config.outer_product_dim
        c_z = config.pair_dim
        self.ln = nn.LayerNorm(c)
        self.lin1 = LinearNoBias(c, c)
        self.lin2 = LinearNoBias(c, c)
        self.lin3 = Linear(c*c, c_z)

    def forward(self, m):
        m = self.ln(m)
        a, b = self.lin1(m), self.lin2(m)
        o = torch.outer(a, b).mean().flatten()
        z = self.lin3(o)
        return z


# Algorithm 10
class MSAPairWeightedAveraging(nn.Module):
    def __init__(self, config):
        super().__init__()
        dim_head = config.msa_pwa_dim_head
        heads = config.msa_pwa_heads
        dim_msa = config.dim_msa
        dim_pair = config.dim_pair
        dropout = config.msa_pwa_dropout_row_prob
        dim_inner = dim_head * heads

        self.msa_to_values_and_gates = nn.Sequential(
            nn.LayerNorm(dim_msa),
            LinearNoBias(dim_msa, dim_inner * 2),
            Rearrange('b s n (gv h d) -> gv b h s n d', gv=2, h=heads)
        )

        self.pairwise_repr_to_attn = nn.Sequential(
            nn.LayerNorm(dim_pair),
            LinearNoBias(dim_pair, heads),
            Rearrange('b i j h -> b h i j')
        )

        self.to_out = nn.Sequential(
            Rearrange('b h s n d -> b s n (h d)'),
            LinearNoBias(dim_inner, dim_msa),
            nn.Dropout(dropout)
        )

    def forward(self, msa, pairwise_repr, mask=None):
        # (b, n_alignments, seq_len, msa_embed_dim)
        # (b, seq_len, seq_len, pair_dim)
        # (b, seq_len)
        values, gates = self.msa_to_values_and_gates(msa)
        gates = torch.sigmoid(gates)

        # Line 3
        b = self.pairwise_repr_to_attn(pairwise_repr)

        if mask is not None:
            mask = mask.unsqueeze(1).unsqueeze(2)
            b = b.masked_fill(~mask, float('-inf'))

        # Line 5
        weights = torch.softmax(b, dim=-1)

        # Line 6
        out = torch.einsum('b h i j, b h s j d -> b h s i d', weights, values)

        out = out * gates

        # Combine heads
        return self.to_out(out)


# Algorithm 11
class Transition(nn.Module):
    def __init__(self, hidden_dim, expansion=4):
        super().__init__()
        intermediate_dim = int(hidden_dim * expansion)
        self.ln = nn.LayerNorm(hidden_dim)
        self.up_a = LinearNoBias(hidden_dim, intermediate_dim)
        self.up_b = LinearNoBias(hidden_dim, intermediate_dim)
        self.down = LinearNoBias(intermediate_dim, hidden_dim)

    def forward(self, x):
        x = self.ln(x)
        a, b = self.up_a(x), self.up_b(x)
        x = self.down(a.mul(b))
        return x


# Algorithm 12 and 13
class TriangleMultiplication(nn.Module):
    def __init__(self, dim, dim_hidden=None, mix='incoming', dropout=0.0):
        super().__init__()

        dim_hidden = dim_hidden if dim_hidden is not None else dim
        self.norm = nn.LayerNorm(dim)

        self.left_proj = LinearNoBias(dim, dim_hidden)
        self.right_proj = LinearNoBias(dim, dim_hidden)

        self.left_gate = LinearNoBias(dim, dim_hidden)
        self.right_gate = LinearNoBias(dim, dim_hidden)
        self.out_gate = LinearNoBias(dim, dim_hidden)

        # initialize all gating to be identity
        for gate in (self.left_gate, self.right_gate, self.out_gate):
            nn.init.constant_(gate.weight, 0.)
            nn.init.constant_(gate.bias, 1.)

        if mix == 'outgoing':
            self.mix_einsum_eq = '... i k d, ... j k d -> ... i j d'
        elif mix == 'incoming':
            self.mix_einsum_eq = '... k j d, ... k i d -> ... i j d'

        self.to_out_norm = nn.LayerNorm(dim_hidden)

        self.to_out = nn.Sequential(
            LinearNoBias(dim_hidden, dim),
            nn.Dropout(dropout)
        )

    def forward(self, x, mask=None):
        if mask is not None:
            mask = mask.unsqueeze(-1).unsqueeze(-1) & mask.unsqueeze(-1).unsqueeze(1)

        x = self.norm(x)

        left = self.left_proj(x)
        right = self.right_proj(x)

        if mask is not None:
            left = left * mask
            right = right * mask

        left_gate = torch.sigmoid(self.left_gate(x))
        right_gate = torch.sigmoid(self.right_gate(x))
        out_gate = torch.sigmoid(self.out_gate(x))

        left = left * left_gate
        right = right * right_gate

        out = torch.einsum(self.mix_einsum_eq, left, right)

        out = self.to_out_norm(out)
        out = out * out_gate
        return self.to_out(out)


# Algorithm 14 and 15
class TriangleAttention(nn.Module):
    def __init__(self, dim, heads, node_type, dropout=0., **attn_kwargs):
        super().__init__()
        self.need_transpose = node_type == 'ending'

        self.attn = Attention(dim=dim, heads=heads, **attn_kwargs)

        self.dropout = nn.Dropout(dropout)

        self.to_attn_bias = nn.Sequential(
            LinearNoBias(dim, heads),
            nn.Unflatten(dim=-1, unflattened_size=(heads, -1))
        )

    def forward(self, pairwise_repr, mask=None, **kwargs):
        if self.need_transpose:
            pairwise_repr = rearrange(pairwise_repr, 'b i j d -> b j i d')

        attn_bias = self.to_attn_bias(pairwise_repr)

        batch_repeat = pairwise_repr.shape[1]
        attn_bias = repeat(attn_bias, 'b ... -> (b r) ...', r=batch_repeat)

        if mask is not None:
            mask = repeat(mask, 'b ... -> (b r) ...', r=batch_repeat)

        pairwise_repr_flat = rearrange(pairwise_repr, 'b n d -> (b n) d')

        out = self.attn(pairwise_repr_flat, mask=mask, attn_bias=attn_bias, **kwargs)

        out = rearrange(out, '(b n) d -> b n d', b=pairwise_repr.shape[0])

        if self.need_transpose:
            out = rearrange(out, 'b j i d -> b i j d')

        return self.dropout(out)


# Algorithm 16
class TemplateEmbedder(nn.Module):
    def __init__(
        self,
        dim: int = None,
        depth: int = 2,
        seq_len: int = None,
        heads: int = 64,
        dim_head: int = 64,
        attn_dropout: float = 0.0,
        ff_dropout: float = 0.0,
        global_column_attn: bool = False,
        c: int = 64,
        Ntemplates: int = 1,
        *args,
        **kwargs,
    ):
        super(TemplateEmbedder, self).__init__()
        self.layer_norm_z = nn.LayerNorm(c)
        self.layer_norm_v = nn.LayerNorm(c)
        self.linear_no_bias_z = LinearNoBias(c, c)
        self.linear_no_bias_a = LinearNoBias(c, c)
        self.pairformer = PairformerStack(
            dim=dim,
            seq_len=seq_len,
            heads=heads,
            dim_head=dim_head,
            attn_dropout=attn_dropout,
            ff_dropout=ff_dropout,
            depth=depth,
            *args,
            **kwargs,
        )
        self.relu = nn.ReLU()
        self.final_linear = LinearNoBias(c, c)

    def forward(self, f, zij, N_templates):
        # Compute various masks and concatenate
        template_backbone_frame_mask = f  # Placeholder operation
        template_pseudo_beta_mask = f  # Placeholder operation
        template_distogram = f  # Placeholder operation
        template_unit_vector = f  # Placeholder operation

        atij = torch.cat(
            [
                template_distogram,
                template_backbone_frame_mask,
                template_unit_vector,
                template_pseudo_beta_mask,
            ],
            dim=-1,
        )

        # Apply masking based on asym_id and concatenate restypes
        asym_id_mask = f == f  # Placeholder for actual asym_id comparison logic
        atij = atij * asym_id_mask
        restype = f  # Placeholder for restype feature
        atij = torch.cat([atij, restype, restype], dim=-1)

        # Initialize uij
        uij = torch.zeros_like(atij)

        # Iterate over templates
        for _ in range(N_templates):
            vij = self.linear_no_bias_z(self.layer_norm_z(zij)) + self.linear_no_bias_a(atij)
            for layer in self.pairformer.layers:
                vij = layer(vij)  # Assuming some residual connection or similar logic in actual Pairformer
            uij += self.layer_norm_v(vij)

        # Normalize and apply final transformation
        uij /= N_templates
        uij = self.final_linear(self.relu(uij))

        return uij


# Algorithm 17
class PairwiseBlock(nn.Module):
    def __init__(self, config):
        super().__init__()
        dim_pair = config.dim_pair
        dropout_row_prob = config.dropout_row_prob
        dropout_col_prob = config.dropout_col_prob
        pre_ln = partial(nn.LayerNorm, normalized_shape=dim_pair)

        tri_mult_kwargs = {
            'dim': dim_pair,
            'dim_hidden': config.tri_mult_dim_hidden
        }

        tri_attn_kwargs = {
            'dim': dim_pair,
            'heads': config.tri_attn_heads,
            'dim_head': config.tri_attn_dim_head
        }

        self.tri_mult_outgoing = nn.Sequential(
            pre_ln(TriangleMultiplication(mix='outgoing', dropout=dropout_row_prob, **tri_mult_kwargs))
        )
        self.tri_mult_incoming = nn.Sequential(
            pre_ln(TriangleMultiplication(mix='incoming', dropout=dropout_row_prob, **tri_mult_kwargs))
        )
        self.tri_attn_starting = nn.Sequential(
            pre_ln(TriangleAttention(node_type='starting', dropout=dropout_row_prob, **tri_attn_kwargs))
        )
        self.tri_attn_ending = nn.Sequential(
            pre_ln(TriangleAttention(node_type='ending', dropout=dropout_col_prob, **tri_attn_kwargs))
        )
        self.pairwise_transition = nn.Sequential(
            pre_ln(Transition(dim_pair))
        )

    def forward(self, pairwise_repr, mask=None):
        pairwise_repr = self.tri_mult_outgoing(pairwise_repr, mask=mask) + pairwise_repr
        pairwise_repr = self.tri_mult_incoming(pairwise_repr, mask=mask) + pairwise_repr
        pairwise_repr = self.tri_attn_starting(pairwise_repr, mask=mask) + pairwise_repr
        pairwise_repr = self.tri_attn_ending(pairwise_repr, mask=mask) + pairwise_repr

        pairwise_repr = self.pairwise_transition(pairwise_repr) + pairwise_repr
        return pairwise_repr


class PairformerStack(nn.Module):
    def __init__(self, config, pairwise_block_kwargs=None):
        super().__init__()

        if pairwise_block_kwargs is None:
            pairwise_block_kwargs = {}

        pair_bias_attn_kwargs = {
            'dim': config.dim_single,
            'dim_pair': config.dim_pair,
            'heads': config.pair_bias_attn_heads,
            'dim_head': config.pair_bias_attn_dim_head,
            'dropout': config.dropout_row_prob
        }

        self.layers = nn.ModuleList([])
        for _ in range(config.pairformer_depth):
            single_pre_ln = partial(nn.LayerNorm, normalized_shape=config.dim_single)
            pairwise_block = PairwiseBlock(dim_pair=config.dim_pair, **pairwise_block_kwargs)
            pair_bias_attn = AttentionPairBias(**pair_bias_attn_kwargs)
            single_transition = Transition(config.dim_single)

            self.layers.append(nn.ModuleList([
                pairwise_block,
                single_pre_ln(pair_bias_attn),
                single_pre_ln(single_transition)
            ]))

    def forward(self, single_repr, pairwise_repr, mask=None):
        for pairwise_block, pair_bias_attn, single_transition in self.layers:
            pairwise_repr = pairwise_block(pairwise_repr=pairwise_repr, mask=mask)

            single_repr = pair_bias_attn(single_repr, pairwise_repr=pairwise_repr, mask=mask) + single_repr
            single_repr = single_transition(single_repr) + single_repr

        return single_repr, pairwise_repr


# Algorithm 18
def SampleDiffusion():
    pass


# Algorithm 19
class CentreRandomAugmentation(nn.Module):
    def __init__(self, s_trans: float = 1.0):
        super().__init__()
        self.s_trans = s_trans

    def forward(self, x_l: torch.Tensor) -> torch.Tensor:
        x_l = x_l - x_l.mean(dim=-2, keepdim=True)
        R = self.uniform_random_rotation()
        t = self.s_trans * torch.randn(3)
        x_l = torch.einsum("ij,jk->ik", R, x_l) + t
        return x_l

    def uniform_random_rotation(self) -> torch.Tensor:
        random_matrix = torch.randn(3, 3)
        q, r = torch.linalg.qr(random_matrix)
        d = torch.diag(r).sign()
        q *= d.unsqueeze(0)
        return q


# Algorithm 20
class DiffusionModule(nn.Module):
    def __init__(
        self,
        dim_pair_trunk,
        dim_pair_rel_pos_feats,
        atoms_per_window=27,
        dim_pair=128,
        sigma_data=16,
        dim_atom=128,
        dim_atompair=16,
        dim_token=768,
        dim_single=384,
        dim_fourier=256,
        single_cond_kwargs=None,
        pairwise_cond_kwargs=None,
        atom_encoder_depth=3,
        atom_encoder_heads=4,
        token_transformer_depth=24,
        token_transformer_heads=16,
        atom_decoder_depth=3,
        atom_decoder_heads=4
    ):
        super().__init__()
        if single_cond_kwargs is None:
            single_cond_kwargs = {
                'num_transitions': 2,
                'transition_expansion_factor': 2
            }
        if pairwise_cond_kwargs is None:
            pairwise_cond_kwargs = {'num_transitions': 2}

        self.atoms_per_window = atoms_per_window

        self.single_conditioner = SingleConditioning(
            sigma_data=sigma_data,
            dim_single=dim_single,
            dim_fourier=dim_fourier,
            **single_cond_kwargs
        )

        self.pairwise_conditioner = PairwiseConditioning(
            dim_pair_trunk=dim_pair_trunk,
            dim_pair_rel_pos_feats=dim_pair_rel_pos_feats,
            **pairwise_cond_kwargs
        )

        self.atom_pos_to_atom_feat_cond = nn.Linear(3, dim_atom, bias=False)

        self.atom_encoder = DiffusionTransformer(
            dim=dim_atom,
            dim_single_cond=dim_atom,
            dim_pair=dim_atompair,
            attn_window_size=atoms_per_window,
            depth=atom_encoder_depth,
            heads=atom_encoder_heads
        )

        self.cond_tokens_with_cond_single = nn.Sequential(
            nn.LayerNorm(dim_single),
            nn.Linear(dim_single, dim_atom, bias=False)
        )

        self.token_transformer = DiffusionTransformer(
            dim=dim_atom,
            dim_single_cond=dim_single,
            dim_pair=dim_pair,
            depth=token_transformer_depth,
            heads=token_transformer_heads
        )

        self.attended_token_norm = nn.LayerNorm(dim_atom)

        self.tokens_to_atom_decoder_input_cond = nn.Linear(dim_atom, dim_atom, bias=False)

        self.atom_decoder = DiffusionTransformer(
            dim=dim_atom,
            dim_single_cond=dim_atom,
            dim_pair=dim_atompair,
            attn_window_size=atoms_per_window,
            depth=atom_decoder_depth,
            heads=atom_decoder_heads
        )

        self.atom_feat_to_atom_pos_update = nn.Sequential(
            nn.LayerNorm(dim_atom),
            nn.Linear(dim_atom, 3, bias=False)
        )

    def forward(
        self,
        noised_atom_pos,
        atom_feats,
        atompair_feats,
        atom_mask,
        times,
        mask,
        single_trunk_repr,
        single_inputs_repr,
        pairwise_trunk,
        pairwise_rel_pos_feats
    ):
        assert noised_atom_pos.shape[-2] % self.atoms_per_window == 0

        conditioned_single_repr = self.single_conditioner(
            times=times,
            single_trunk_repr=single_trunk_repr,
            single_inputs_repr=single_inputs_repr
        )

        conditioned_pairwise_repr = self.pairwise_conditioner(
            pairwise_trunk=pairwise_trunk,
            pairwise_rel_pos_feats=pairwise_rel_pos_feats
        )

        atom_feats = self.atom_pos_to_atom_feat_cond(noised_atom_pos) + atom_feats

        atom_feats = self.atom_encoder(
            atom_feats,
            mask=atom_mask,
            single_repr=atom_feats,
            pairwise_repr=atompair_feats
        )

        atom_feats_skip = atom_feats

        w = self.atoms_per_window
        windowed_atom_feats = atom_feats.view(-1, mask.shape[1], w, atom_feats.shape[-1])
        windowed_atom_mask = atom_mask.view(-1, mask.shape[1], w)

        assert windowed_atom_mask.any(dim=-1).all(), 'atom mask must contain one valid atom for each window'

        windowed_atom_feats = windowed_atom_feats.masked_fill(windowed_atom_mask.unsqueeze(-1), 0.)

        num = windowed_atom_feats.sum(dim=-2)
        den = windowed_atom_mask.float().sum(dim=-1, keepdim=True)

        tokens = num / den

        tokens = self.cond_tokens_with_cond_single(conditioned_single_repr) + tokens

        self.token_transformer(
            tokens,
            mask=mask,
            single_repr=conditioned_single_repr,
            pairwise_repr=conditioned_pairwise_repr,
        )

        tokens = self.attended_token_norm(tokens)

        atom_decoder_input = self.tokens_to_atom_decoder_input_cond(tokens)
        atom_decoder_input = atom_decoder_input.unsqueeze(2).repeat(1, 1, w, 1).view(-1, mask.shape[1] * w, atom_decoder_input.shape[-1])

        atom_decoder_input = atom_decoder_input + atom_feats_skip

        atom_feats = self.atom_decoder(
            atom_feats,
            mask=atom_mask,
            single_repr=atom_feats,
            pairwise_repr=atompair_feats
        )

        atom_pos_update = self.atom_feat_to_atom_pos_update(atom_feats)

        return atom_pos_update


# Algorithm 21
class ConditionWrapper(nn.Module):
    def __init__(self, fn, dim, dim_cond, adaln_zero_bias_init_value=-2.):
        super().__init__()
        self.fn = fn
        self.adaptive_norm = AdaptiveLayerNorm(dim, dim_cond)

        self.to_adaln_zero_gamma = nn.Sequential(
            nn.Linear(dim_cond, dim),
            nn.Sigmoid()
        )
        nn.init.zeros_(self.to_adaln_zero_gamma[0].weight)
        nn.init.constant_(self.to_adaln_zero_gamma[0].bias, adaln_zero_bias_init_value)

    def forward(self, x, cond, **kwargs):
        x = self.adaptive_norm(x, cond=cond)
        out = self.fn(x, **kwargs)
        gamma = self.to_adaln_zero_gamma(cond)
        return out * gamma


class PairwiseConditioning(nn.Module):
    def __init__(
        self,
        dim_pair_trunk,
        dim_pair_rel_pos_feats,
        dim_pair=128,
        num_transitions=2,
        transition_expansion_factor=2,
    ):
        super().__init__()

        self.dim_pair_init_proj = nn.Sequential(
            nn.Linear(dim_pair_trunk + dim_pair_rel_pos_feats, dim_pair, bias=False),
            nn.LayerNorm(dim_pair)
        )

        self.transitions = nn.ModuleList([
            nn.Sequential(
                nn.LayerNorm(dim_pair),
                Transition(dim_pair, expansion_factor=transition_expansion_factor)
            )
            for _ in range(num_transitions)
        ])

    def forward(self, pairwise_trunk, pairwise_rel_pos_feats):
        pairwise_repr = torch.cat((pairwise_trunk, pairwise_rel_pos_feats), dim=-1)
        pairwise_repr = self.dim_pair_init_proj(pairwise_repr)

        for transition in self.transitions:
            pairwise_repr = transition(pairwise_repr) + pairwise_repr

        return pairwise_repr


class SingleConditioning(nn.Module):
    def __init__(
        self,
        sigma_data,
        dim_single=384,
        dim_fourier=256,
        num_transitions=2,
        transition_expansion_factor=2,
        eps=1e-20
    ):
        super().__init__()
        self.eps = eps
        self.dim_single = dim_single
        self.sigma_data = sigma_data

        self.norm_single = nn.LayerNorm(dim_single)

        self.fourier_embed = FourierEmbedding(dim_fourier)
        self.norm_fourier = nn.LayerNorm(dim_fourier)
        self.fourier_to_single = nn.Linear(dim_fourier, dim_single, bias=False)

        self.transitions = nn.ModuleList([
            nn.Sequential(
                nn.LayerNorm(dim_single),
                Transition(dim_single, expansion_factor=transition_expansion_factor)
            )
            for _ in range(num_transitions)
        ])

    def forward(self, times, single_trunk_repr, single_inputs_repr):
        single_repr = torch.cat((single_trunk_repr, single_inputs_repr), dim=-1)
        assert single_repr.shape[-1] == self.dim_single

        single_repr = self.norm_single(single_repr)

        fourier_embed = self.fourier_embed(0.25 * torch.log(torch.clamp(times / self.sigma_data, min=self.eps)))
        normed_fourier = self.norm_fourier(fourier_embed)
        fourier_to_single = self.fourier_to_single(normed_fourier)

        single_repr = fourier_to_single.unsqueeze(1) + single_repr

        for transition in self.transitions:
            single_repr = transition(single_repr) + single_repr

        return single_repr


# Algorithm 22
class FourierEmbedding(nn.Module):
    def __init__(self, dim):
        super().__init__()
        self.proj = Linear(1, dim)
        self.proj.requires_grad_(False)

    def forward(self, times):
        times = times.unsqueeze(-1)
        rand_proj = self.proj(times)
        return torch.cos(2 * torch.pi * rand_proj)


# Algorithm 23
class DiffusionTransformer(nn.Module):
    def __init__(
        self,
        depth,
        heads,
        dim=384,
        dim_single_cond=None,
        dim_pair=128,
        attn_window_size=None,
        attn_pair_bias_kwargs=None
    ):
        super().__init__()
        if dim_single_cond is None:
            dim_single_cond = dim
        if attn_pair_bias_kwargs is None:
            attn_pair_bias_kwargs = {}

        self.layers = nn.ModuleList([])
        for _ in range(depth):
            pair_bias_attn = AttentionPairBias(
                dim=dim,
                dim_pair=dim_pair,
                heads=heads,
                window_size=attn_window_size,
                **attn_pair_bias_kwargs
            )
            transition = Transition(dim)
            conditionable_pair_bias = ConditionWrapper(
                pair_bias_attn,
                dim=dim,
                dim_cond=dim_single_cond
            )
            conditionable_transition = ConditionWrapper(
                transition,
                dim=dim,
                dim_cond=dim_single_cond
            )
            self.layers.append(nn.ModuleList([
                conditionable_pair_bias,
                conditionable_transition
            ]))

    def forward(self, noised_repr, single_repr, pairwise_repr, mask=None):
        for attn, transition in self.layers:
            attn_out = attn(
                noised_repr,
                cond=single_repr,
                pairwise_repr=pairwise_repr,
                mask=mask
            )
            ff_out = transition(noised_repr, cond=single_repr)
            noised_repr = noised_repr + attn_out + ff_out
        return noised_repr


# Algorithm 24
class AttentionPairBias(nn.Module):
    def __init__(self, heads, dim_pair, window_size=None, **attn_kwargs):
        super().__init__()

        self.attn = Attention(heads=heads, window_size=window_size, **attn_kwargs)

        to_attn_bias_linear = nn.Linear(dim_pair, heads, bias=False)
        nn.init.zeros_(to_attn_bias_linear.weight)

        self.to_attn_bias = nn.Sequential(
            nn.LayerNorm(dim_pair),
            to_attn_bias_linear,
            Rearrange('... i j h -> ... h i j')
        )

    def forward(self, single_repr, pairwise_repr, attn_bias=None, **kwargs):
        if attn_bias is not None:
            attn_bias = attn_bias.unsqueeze(1)
        else:
            attn_bias = 0.0

        attn_bias = self.to_attn_bias(pairwise_repr) + attn_bias

        out = self.attn(single_repr, attn_bias=attn_bias, **kwargs)

        return out


# Algorithm 25
class ConditionedTransitionBlock(nn.Module):
    def __init__(self, dim, dim_cond):
        super().__init__()
        self.adaln = AdaptiveLayerNorm(dim, dim_cond)
        self.lin1 = LinearNoBias(dim, dim)
        self.lin2 = LinearNoBias(dim, dim)
        self.lin3 = Linear(dim, dim)
        self.lin4 = LinearNoBias(dim, dim)

        self.lin3.bias.data.fill_(-2.0)
    
    def forward(self, a, s):
        a = self.adaln(a, s)
        b = F.silu(self.lin1(a)).mul(self.lin2(a))
        a = self.lin3(s).sigmoid().mul(self.lin4(b))
        return a


# Algorithm 26
class AdaptiveLayerNorm(nn.Module):
    def __init__(self, dim, dim_cond):
        super().__init__()
        self.norm = nn.LayerNorm(dim, elementwise_affine=False)
        self.norm_cond = nn.LayerNorm(dim_cond, elementwise_affine=False)

        self.lin1 = Linear(dim, dim)
        self.lin2 = LinearNoBias(dim, dim)

    def forward(self, a, s):
        a = self.norm(a)
        s = self.norm_cond(s)
        a = self.lin1(s).sigmoid().mul(a) + self.lin2(s)
        return a


# Algorithm 27
class SmoothLDDTLoss(nn.Module):
    """
    Smooth LDDT Loss (Algorithm 27)

    This loss function computes the smooth LDDT (Local Distance Difference Test) loss
    between predicted and true coordinates of atoms in a molecule.

    Args:
        nucleic_acid_cutoff (float): Inclusion radius cutoff for nucleic acid atoms (default: 30.0)
        other_cutoff (float): Inclusion radius cutoff for other atoms (default: 15.0)
    """

    def __init__(self, nucleic_acid_cutoff=30.0, other_cutoff=15.0):
        super().__init__()
        self.nucleic_acid_cutoff = nucleic_acid_cutoff
        self.other_cutoff = other_cutoff

    def forward(self, pred_coords, true_coords, is_dna, is_rna):
        """
        Compute the smooth LDDT loss.

        Args:
            pred_coords: Predicted coordinates (b, n, 3)
            true_coords: True coordinates (b, n, 3)
            is_dna: Boolean tensor indicating DNA atoms (b, n)
            is_rna: Boolean tensor indicating RNA atoms (b, n)

        Returns:
            Smooth LDDT loss value
        """
        # Compute distances between all pairs of atoms
        pred_dists = torch.cdist(pred_coords, pred_coords)
        true_dists = torch.cdist(true_coords, true_coords)

        # Compute distance difference for all pairs of atoms
        dist_diff = torch.abs(true_dists - pred_dists)

        # Compute epsilon values
        eps = (
            F.sigmoid(0.5 - dist_diff) +
            F.sigmoid(1.0 - dist_diff) +
            F.sigmoid(2.0 - dist_diff) +
            F.sigmoid(4.0 - dist_diff)
        ) / 4.0

        # Restrict to bespoke inclusion radius
        is_nucleotide = is_dna | is_rna
        is_nucleotide_pair = is_nucleotide.unsqueeze(-1) & is_nucleotide.unsqueeze(-2)
        inclusion_radius = torch.where(
            is_nucleotide_pair,
            true_dists < self.nucleic_acid_cutoff,
            true_dists < self.other_cutoff
        )

        # Compute mean, avoiding self term
        mask = torch.logical_and(inclusion_radius, torch.logical_not(torch.eye(pred_coords.shape[1], dtype=torch.bool, device=pred_coords.device)))
        lddt_sum = (eps * mask).sum(dim=(-1, -2))
        lddt_count = mask.sum(dim=(-1, -2))
        lddt = lddt_sum / lddt_count.clamp(min=1)

        return 1 - lddt.mean()


# Algorithm 28
class WeightedRigidAlign(nn.Module):
    """
    Weighted Rigid Alignment (Algorithm 28)

    This module performs weighted rigid alignment between predicted and true coordinates.

    Args:
        None
    """

    def __init__(self):
        super().__init__()

    def forward(self, pred_coords, true_coords, weights):
        """
        Perform weighted rigid alignment.

        Args:
            pred_coords: Predicted coordinates (b, n, 3)
            true_coords: True coordinates (b, n, 3)
            weights: Weights for each atom (b, n)

        Returns:
            Aligned predicted coordinates
        """
        # Compute weighted centroids
        pred_centroid = (pred_coords * weights.unsqueeze(-1)).sum(dim=1) / weights.sum(dim=1, keepdim=True)
        true_centroid = (true_coords * weights.unsqueeze(-1)).sum(dim=1) / weights.sum(dim=1, keepdim=True)

        # Center the coordinates
        pred_coords_centered = pred_coords - pred_centroid.unsqueeze(1)
        true_coords_centered = true_coords - true_centroid.unsqueeze(1)

        # Compute the weighted covariance matrix
        cov_matrix = torch.einsum('bni,bnj->bij', true_coords_centered * weights.unsqueeze(-1), pred_coords_centered)

        # Compute the SVD of the covariance matrix
        U, _, V = torch.svd(cov_matrix)

        # Compute the rotation matrix
        rot_matrix = torch.einsum('bij,bjk->bik', U, V)

        # Ensure proper rotation matrix with determinant 1
        det = torch.det(rot_matrix)
        det_mask = det < 0
        V_fixed = V.clone()
        V_fixed[det_mask, :, -1] *= -1
        rot_matrix[det_mask] = torch.einsum('bij,bjk->bik', U[det_mask], V_fixed[det_mask])

        # Apply the rotation and translation
        aligned_coords = torch.einsum('bni,bij->bnj', pred_coords_centered, rot_matrix) + true_centroid.unsqueeze(1)

        return aligned_coords.detach()


# Algorithm 29
def expressCoordinatesInFrame(x, phi):
    a, b, c = phi
    # Extract frame atoms
    w1 = (a - b) / torch.norm(a - b)
    w2 = (c - b) / torch.norm(c - b)
    # Build orthonormal basis
    e1 = (w1 + w2) / torch.norm(w1 + w2)
    e2 = (w2 - w1) / torch.norm(w2 - w1)
    e3 = e1 * e2
    # Project onto frame basis
    d = x - b
    xt = torch.concat([d.dot(e1), d.dot(e2), d.dot(e3)])
    return xt


# Algorithm 30
def computeAlignmentError(x, x_true, phi, phi_true, eps=1e-8):
    x_bar = expressCoordinatesInFrame(x, phi)
    x_bar_true = expressCoordinatesInFrame(x_true, phi_true)
    error = torch.sqrt(torch.square(x_bar - x_bar_true) + eps)
    return error


# Algorithm 31
class ConfidenceHead(nn.Module):
    def __init__(self, config, pairformer_kwargs=None):
        super().__init__()
        if pairformer_kwargs is None:
            pairformer_kwargs = {}
        dim_pair = config.dim_pair
        dim_single = config.dim_single
            
        self.register_buffer('atompair_dist_bins', config.atompair_dist_bins)
        num_dist_bins = config.atompair_dist_bins.shape[-1]
        self.num_dist_bins = num_dist_bins

        self.dist_bin_pairwise_embed = nn.Embedding(num_dist_bins, dim_pair)
        self.single_inputs_to_pairwise = LinearNoBias(config.dim_single_inputs, dim_pair * 2)

        self.pairformer_stack = PairformerStack(
            dim_single=dim_single,
            dim_pair=dim_pair,
            depth=config.confidence_depth,
            **pairformer_kwargs
        )

        self.to_pae_logits = LinearNoBias(dim_pair, config.num_pae_bins)
        self.to_pde_logits = LinearNoBias(dim_pair, config.num_pde_bins)
        self.to_plddt_logits = LinearNoBias(dim_single, config.num_plddt_bins)
        self.to_resolved_logits = LinearNoBias(dim_single, 2)

    def forward(
        self,
        single_inputs_repr,
        single_repr,
        pairwise_repr,
        pred_atom_pos,
        mask=None
    ):
        single_to_pairwise_i, single_to_pairwise_j = self.single_inputs_to_pairwise(single_inputs_repr).chunk(2, dim=-1)
        pairwise_repr = pairwise_repr + single_to_pairwise_i.unsqueeze(2) + single_to_pairwise_j.unsqueeze(1)

        interatom_dist = torch.cdist(pred_atom_pos, pred_atom_pos, p=2)

        dist_bin_indices = (interatom_dist[..., None] - self.atompair_dist_bins).abs().argmin(dim=-1)
        pairwise_repr = pairwise_repr + self.dist_bin_pairwise_embed(dist_bin_indices)

        single_repr, pairwise_repr = self.pairformer_stack(
            single_repr=single_repr,
            pairwise_repr=pairwise_repr,
            mask=mask
        )

        pae_logits = self.to_pae_logits(pairwise_repr)

        symmetric_pairwise_repr = pairwise_repr + pairwise_repr.transpose(1, 2)
        pde_logits = self.to_pde_logits(symmetric_pairwise_repr)

        plddt_logits = self.to_plddt_logits(single_repr)
        resolved_logits = self.to_resolved_logits(single_repr)

        return pae_logits, pde_logits, plddt_logits, resolved_logits




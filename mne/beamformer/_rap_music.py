"""Compute a Recursively Applied and Projected MUltiple Signal Classification (RAP-MUSIC)."""  # noqa

# Authors: The MNE-Python contributors.
# License: BSD-3-Clause
# Copyright the MNE-Python contributors.

import numpy as np
from scipy import linalg

from .._fiff.pick import pick_channels_forward, pick_info
from ..fixes import _safe_svd
from ..forward import convert_forward_solution, is_fixed_orient
from ..inverse_sparse.mxne_inverse import _make_dipoles_sparse
from ..minimum_norm.inverse import _log_exp_var
from ..utils import _check_info_inv, fill_doc, logger, verbose
from ._compute_beamformer import _prepare_beamformer_input


@fill_doc
def _apply_rap_music(
    data, info, times, forward, noise_cov, n_dipoles=2, picks=None, use_trap=False
):
    """RAP-MUSIC or TRAP-MUSIC for evoked data.

    Parameters
    ----------
    data : array, shape (n_channels, n_times)
        Evoked data.
    %(info_not_none)s
    times : array
        Times.
    forward : instance of Forward
        Forward operator.
    noise_cov : instance of Covariance
        The noise covariance.
    n_dipoles : int
        The number of dipoles to estimate. The default value is 2.
    picks : list of int
        Caller ensures this is a list of int.
    use_trap : bool
        Use the TRAP-MUSIC variant if True (default False).

    Returns
    -------
    dipoles : list of instances of Dipole
        The dipole fits.
    explained_data : array | None
        Data explained by the dipoles using a least square fitting with the
        selected active dipoles and their estimated orientation.
    """
    info = pick_info(info, picks)
    del picks
    # things are much simpler if we avoid surface orientation
    align = forward["source_nn"].copy()
    if forward["surf_ori"] and not is_fixed_orient(forward):
        forward = convert_forward_solution(forward, surf_ori=False)
    is_free_ori, info, _, _, G, whitener, _, _ = _prepare_beamformer_input(
        info, forward, noise_cov=noise_cov, rank=None
    )
    forward = pick_channels_forward(forward, info["ch_names"], ordered=True)
    del info

    # whiten the data (leadfield already whitened)
    M = np.dot(whitener, data)
    del data

    _, eig_vectors = linalg.eigh(np.dot(M, M.T))
    phi_sig = eig_vectors[:, -n_dipoles:]

    n_orient = 3 if is_free_ori else 1
    G.shape = (G.shape[0], -1, n_orient)
    gain = forward["sol"]["data"].copy()
    gain.shape = G.shape
    n_channels = G.shape[0]
    A = np.empty((n_channels, n_dipoles))
    gain_dip = np.empty((n_channels, n_dipoles))
    oris = np.empty((n_dipoles, 3))
    poss = np.empty((n_dipoles, 3))

    G_proj = G.copy()
    phi_sig_proj = phi_sig.copy()

    idxs = list()
    for k in range(n_dipoles):
        subcorr_max = -1.0
        source_idx, source_ori, source_pos = 0, [0, 0, 0], [0, 0, 0]
        for i_source in range(G.shape[1]):
            Gk = G_proj[:, i_source]
            subcorr, ori = _compute_subcorr(Gk, phi_sig_proj)
            if subcorr > subcorr_max:
                subcorr_max = subcorr
                source_idx = i_source
                source_ori = ori
                source_pos = forward["source_rr"][i_source]
                if n_orient == 3 and align is not None:
                    surf_normal = forward["source_nn"][3 * i_source + 2]
                    # make sure ori is aligned to the surface orientation
                    source_ori *= np.sign(source_ori @ surf_normal) or 1.0
                if n_orient == 1:
                    source_ori = forward["source_nn"][i_source]

        idxs.append(source_idx)
        if n_orient == 3:
            Ak = np.dot(G[:, source_idx], source_ori)
        else:
            Ak = G[:, source_idx, 0]
        A[:, k] = Ak
        oris[k] = source_ori
        poss[k] = source_pos

        logger.info(f"source {k + 1} found: p = {source_idx}")
        if n_orient == 3:
            logger.info("ori = {} {} {}".format(*tuple(oris[k])))

        projection = _compute_proj(A[:, : k + 1])
        G_proj = np.einsum("ab,bso->aso", projection, G)
        phi_sig_proj = np.dot(projection, phi_sig)
        if use_trap:
            phi_sig_proj = phi_sig_proj[:, -(n_dipoles - k) :]
    del G, G_proj

    sol = linalg.lstsq(A, M)[0]
    if n_orient == 3:
        X = sol[:, np.newaxis] * oris[:, :, np.newaxis]
        X.shape = (-1, len(times))
    else:
        X = sol

    gain_active = gain[:, idxs]
    if n_orient == 3:
        gain_dip = (oris * gain_active).sum(-1)
        idxs = np.array(idxs)
        active_set = np.array([[3 * idxs, 3 * idxs + 1, 3 * idxs + 2]]).T.ravel()
    else:
        gain_dip = gain_active[:, :, 0]
        active_set = idxs
    gain_active = whitener @ gain_active.reshape(gain.shape[0], -1)
    assert gain_active.shape == (n_channels, X.shape[0])

    explained_data = gain_dip @ sol
    M_estimate = whitener @ explained_data
    _log_exp_var(M, M_estimate)
    tstep = np.median(np.diff(times)) if len(times) > 1 else 1.0
    dipoles = _make_dipoles_sparse(
        X, active_set, forward, times[0], tstep, M, gain_active, active_is_idx=True
    )
    for dipole, ori in zip(dipoles, oris):
        signs = np.sign((dipole.ori * ori).sum(-1, keepdims=True))
        dipole._ori *= signs
        dipole._amplitude *= signs[:, 0]
    logger.info("[done]")
    return dipoles, explained_data


def _compute_subcorr(G, phi_sig):
    """Compute the subspace correlation."""
    Ug, Sg, Vg = _safe_svd(G, full_matrices=False)
    # Now we look at the actual rank of the forward fields
    # in G and handle the fact that it might be rank defficient
    # eg. when using MEG and a sphere model for which the
    # radial component will be truly 0.
    rank = np.sum(Sg > (Sg[0] * 1e-6))
    if rank == 0:
        return 0, np.zeros(len(G))
    rank = max(rank, 2)  # rank cannot be 1
    Ug, Sg, Vg = Ug[:, :rank], Sg[:rank], Vg[:rank]
    tmp = np.dot(Ug.T.conjugate(), phi_sig)
    Uc, Sc, _ = _safe_svd(tmp, full_matrices=False)
    X = np.dot(Vg.T / Sg[None, :], Uc[:, 0])  # subcorr
    return Sc[0], X / np.linalg.norm(X)


def _compute_proj(A):
    """Compute the orthogonal projection operation for a manifold vector A."""
    U, _, _ = _safe_svd(A, full_matrices=False)
    return np.identity(A.shape[0]) - np.dot(U, U.T.conjugate())


def _rap_music(evoked, forward, noise_cov, n_dipoles, return_residual, use_trap):
    """RAP-/TRAP-MUSIC implementation."""
    info = evoked.info
    data = evoked.data
    times = evoked.times

    picks = _check_info_inv(info, forward, data_cov=None, noise_cov=noise_cov)

    data = data[picks]

    dipoles, explained_data = _apply_rap_music(
        data, info, times, forward, noise_cov, n_dipoles, picks, use_trap
    )

    if return_residual:
        residual = evoked.copy().pick([info["ch_names"][p] for p in picks])
        residual.data -= explained_data
        active_projs = [p for p in residual.info["projs"] if p["active"]]
        for p in active_projs:
            p["active"] = False
        residual.add_proj(active_projs, remove_existing=True)
        residual.apply_proj()
        return dipoles, residual
    else:
        return dipoles


@verbose
def rap_music(
    evoked,
    forward,
    noise_cov,
    n_dipoles=5,
    return_residual=False,
    *,
    verbose=None,
):
    """RAP-MUSIC source localization method.

    Compute Recursively Applied and Projected MUltiple SIgnal Classification
    (RAP-MUSIC) :footcite:`MosherLeahy1999,MosherLeahy1996` on evoked data.

    .. note:: The goodness of fit (GOF) of all the returned dipoles is the
              same and corresponds to the GOF of the full set of dipoles.

    Parameters
    ----------
    evoked : instance of Evoked
        Evoked data to localize.
    forward : instance of Forward
        Forward operator.
    noise_cov : instance of Covariance
        The noise covariance.
    n_dipoles : int
        The number of dipoles to look for. The default value is 5.
    return_residual : bool
        If True, the residual is returned as an Evoked instance.
    %(verbose)s

    Returns
    -------
    dipoles : list of instance of Dipole
        The dipole fits.
    residual : instance of Evoked
        The residual a.k.a. data not explained by the dipoles.
        Only returned if return_residual is True.

    See Also
    --------
    mne.fit_dipole
    mne.beamformer.trap_music

    Notes
    -----
    .. versionadded:: 0.9.0

    References
    ----------
    .. footbibliography::
    """
    return _rap_music(evoked, forward, noise_cov, n_dipoles, return_residual, False)


@verbose
def trap_music(
    evoked,
    forward,
    noise_cov,
    n_dipoles=5,
    return_residual=False,
    *,
    verbose=None,
):
    """TRAP-MUSIC source localization method.

    Compute Truncated Recursively Applied and Projected MUltiple SIgnal Classification
    (TRAP-MUSIC) :footcite:`Makela2018` on evoked data.

    .. note:: The goodness of fit (GOF) of all the returned dipoles is the
              same and corresponds to the GOF of the full set of dipoles.

    Parameters
    ----------
    evoked : instance of Evoked
        Evoked data to localize.
    forward : instance of Forward
        Forward operator.
    noise_cov : instance of Covariance
        The noise covariance.
    n_dipoles : int
        The number of dipoles to look for. The default value is 5.
    return_residual : bool
        If True, the residual is returned as an Evoked instance.
    %(verbose)s

    Returns
    -------
    dipoles : list of instance of Dipole
        The dipole fits.
    residual : instance of Evoked
        The residual a.k.a. data not explained by the dipoles.
        Only returned if return_residual is True.

    See Also
    --------
    mne.fit_dipole
    mne.beamformer.rap_music

    Notes
    -----
    .. versionadded:: 1.4

    References
    ----------
    .. footbibliography::
    """
    return _rap_music(evoked, forward, noise_cov, n_dipoles, return_residual, True)

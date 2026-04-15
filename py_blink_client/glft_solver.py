"""
GLFT Finite-Horizon ODE Solver

Implements the exact solution from Gueant, Lehalle & Fernandez-Tapia (2012),
"Dealing with the inventory risk", Proposition 1-2, Theorem 1.

Solves the tridiagonal ODE system:
  v_dot_q = alpha * q^2 * v_q - eta * (v_{q-1} + v_{q+1})   for |q| < Q
  v_dot_Q = alpha * Q^2 * v_Q - eta * v_{Q-1}                at boundary
  v_q(T) = 1

Where: alpha = (k/2) * gamma * sigma^2, eta = A * (1 + gamma/k)^{-(1 + k/gamma)}

Solution: v(t) = exp(-M * tau) * 1, computed via eigendecomposition of M.
Eigendecomposition is O(n^2) for symmetric tridiagonal (one-time).
Per-requote v(t) computation is O(n) exponentials + O(n^2) multiply.

Optimal quotes (Theorem 1):
  delta_b(t,q) = (1/k) * ln(v_q / v_{q+1}) + (1/gamma) * ln(1 + gamma/k)
  delta_a(t,q) = (1/k) * ln(v_q / v_{q-1}) + (1/gamma) * ln(1 + gamma/k)
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass
from typing import List


# ============================================================================
# Symmetric Tridiagonal Eigenvalue Decomposition (QL algorithm with shifts)
# Adapted from Numerical Recipes / EISPACK tqli
# ============================================================================

def tqli(d: List[float], e: List[float], n: int, z: List[List[float]]) -> None:
    """In-place tridiagonal QL eigenvalue decomposition.

    Modifies d (diagonal -> eigenvalues) and z (identity -> eigenvectors)
    in place.  e is the sub-diagonal and is destroyed.

    Faithful port of the TypeScript implementation which itself is adapted
    from Numerical Recipes / EISPACK.

    Args:
        d: Diagonal elements (length n). Overwritten with eigenvalues.
        e: Sub-diagonal elements (length n). Destroyed.
        n: Matrix dimension.
        z: Row-major n x n matrix, initially identity.
           Overwritten with eigenvectors (z[row][col]).
    """
    # Shift sub-diagonal left by one position
    for i in range(1, n):
        e[i - 1] = e[i]
    e[n - 1] = 0.0

    for l in range(n):
        iteration = 0
        while True:
            # Find small sub-diagonal element
            m = l
            while m < n - 1:
                dd = abs(d[m]) + abs(d[m + 1])
                if abs(e[m]) + dd == dd:
                    break
                m += 1

            if m == l:
                break

            iteration += 1
            if iteration > 300:
                raise RuntimeError("GLFT tqli: too many iterations")

            g = (d[l + 1] - d[l]) / (2.0 * e[l])
            r = math.sqrt(g * g + 1.0)
            g = d[m] - d[l] + e[l] / (g + (r if g >= 0 else -r))

            s = 1.0
            c = 1.0
            p = 0.0

            for i in range(m - 1, l - 1, -1):
                f = s * e[i]
                b = c * e[i]
                r = math.sqrt(f * f + g * g)
                e[i + 1] = r
                if r == 0.0:
                    d[i + 1] -= p
                    e[m] = 0.0
                    break
                s = f / r
                c = g / r
                g = d[i + 1] - p
                r = (d[i] - g) * s + 2.0 * c * b
                p = s * r
                d[i + 1] = g + p
                g = c * r - b
                for kk in range(n):
                    f = z[kk][i + 1]
                    z[kk][i + 1] = s * z[kk][i] + c * f
                    z[kk][i] = c * z[kk][i] - s * f
            # These three lines execute unconditionally after the inner
            # for-loop in the TS original (both break and normal cases).
            d[l] -= p
            e[l] = g
            e[m] = 0.0


# ============================================================================
# GLFT Solver
# ============================================================================

@dataclass
class GLFTSolution:
    """Result of solving the GLFT finite-horizon ODE system.

    Provides efficient bid_depth / ask_depth computation for any (q, tau)
    using the precomputed eigendecomposition.
    """
    c1: float            # constant floor (1/gamma) * ln(1 + gamma/k)
    N: int               # matrix dimension (2*Q + 1)
    eigen_time: float    # eigendecomposition time in seconds

    # Internal state for computing depths
    _k: float = 0.0
    _Q: int = 0
    _eigenvalues: List[float] = None  # type: ignore[assignment]
    _Z: List[List[float]] = None      # type: ignore[assignment]
    _coeffs: List[float] = None       # type: ignore[assignment]

    def bid_depth(self, q: int, tau: float) -> float:
        """Compute optimal bid depth in logit space for inventory q at time-to-close tau."""
        Q = self._Q
        q = max(-Q, min(Q, round(q)))
        if q >= Q:
            return float("inf")  # at max inventory, no bid

        i = q + Q
        N = self.N
        eigenvalues = self._eigenvalues
        coeffs = self._coeffs
        Z = self._Z

        # Compute v_q and v_{q+1}
        vq = 0.0
        vq1 = 0.0
        for j in range(N):
            exp_term = coeffs[j] * math.exp(-eigenvalues[j] * tau)
            vq += exp_term * Z[i][j]
            vq1 += exp_term * Z[i + 1][j]

        if vq1 <= 0.0 or vq <= 0.0:
            return float("inf")
        return (1.0 / self._k) * math.log(vq / vq1) + self.c1

    def ask_depth(self, q: int, tau: float) -> float:
        """Compute optimal ask depth in logit space for inventory q at time-to-close tau."""
        Q = self._Q
        q = max(-Q, min(Q, round(q)))
        if q <= -Q:
            return float("inf")  # at min inventory, no ask

        i = q + Q
        N = self.N
        eigenvalues = self._eigenvalues
        coeffs = self._coeffs
        Z = self._Z

        # Compute v_q and v_{q-1}
        vq = 0.0
        vqm1 = 0.0
        for j in range(N):
            exp_term = coeffs[j] * math.exp(-eigenvalues[j] * tau)
            vq += exp_term * Z[i][j]
            vqm1 += exp_term * Z[i - 1][j]

        if vqm1 <= 0.0 or vq <= 0.0:
            return float("inf")
        return (1.0 / self._k) * math.log(vq / vqm1) + self.c1


def solve_glft(
    gamma: float,
    k: float,
    sigma_b: float,
    A: float,
    Q: int,
) -> GLFTSolution:
    """Solve the GLFT finite-horizon ODE system.

    One-time eigendecomposition of the (2Q+1) x (2Q+1) tridiagonal matrix.
    Returns a GLFTSolution that efficiently computes quotes for any (q, tau).

    Args:
        gamma: Risk aversion parameter.
        k: Order arrival decay.
        sigma_b: Belief volatility (logit space).
        A: Order arrival rate at mid-price.
        Q: Max inventory (one side).

    Returns:
        GLFTSolution with bid_depth() and ask_depth() methods.
    """
    N = 2 * Q + 1

    # GLFT Proposition 1: alpha = (k/2)*gamma*sigma^2, eta = A*(1+gamma/k)^{-(1+k/gamma)}
    alpha = (k / 2.0) * gamma * sigma_b * sigma_b
    eta = A * math.pow(1.0 + gamma / k, -(1.0 + k / gamma))
    c1 = (1.0 / gamma) * math.log(1.0 + gamma / k)

    # Build tridiagonal matrix: diagonal = alpha * q^2, off-diagonal = -eta
    diag: List[float] = [0.0] * N
    subdiag: List[float] = [0.0] * N
    for i in range(N):
        q = i - Q
        diag[i] = alpha * q * q
        if i < N - 1:
            subdiag[i + 1] = -eta

    # Identity matrix for eigenvectors
    Z: List[List[float]] = []
    for i in range(N):
        row = [0.0] * N
        row[i] = 1.0
        Z.append(row)

    # Eigendecomposition
    start = time.perf_counter()
    tqli(diag, subdiag, N, Z)
    eigen_time = time.perf_counter() - start

    # Precompute coefficients: coeff_i = sum_j Z[j][i] (dot product with ones vector)
    coeffs: List[float] = [0.0] * N
    for i in range(N):
        s = 0.0
        for j in range(N):
            s += Z[j][i]
        coeffs[i] = s

    # The eigenvalues are in diag[], eigenvectors in columns of Z
    solution = GLFTSolution(
        c1=c1,
        N=N,
        eigen_time=eigen_time,
        _k=k,
        _Q=Q,
        _eigenvalues=diag,
        _Z=Z,
        _coeffs=coeffs,
    )
    return solution

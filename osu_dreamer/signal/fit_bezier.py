# https://github.com/volkerp/fitCurves

import numpy as np

import bezier

def hodo(p: "N,2"):
    return p.shape[0] * (p[1:] - p[:-1])

def q(p: "4,2", t: "L,") -> "L,2":
    """evaluates cubic bezier at t"""
    return bezier.Curve.from_nodes(p.T).evaluate_multi(t).T

def qprime(p: "4,2", t: "L,") -> "L,2":
    """evaluates cubic bezier first derivative at t"""
    return bezier.Curve.from_nodes(hodo(p).T).evaluate_multi(t).T

def qprimeprime(p: "4,2", t: "L,") -> "L,2":
    """evaluates cubic bezier second derivative at t"""
    return bezier.Curve.from_nodes(hodo(hodo(p)).T).evaluate_multi(t).T

def normalize(v):
    magnitude = np.sqrt(np.dot(v,v))
    if magnitude < np.finfo(float).eps:
        return v
    return v / magnitude

def fit_bezier(points: "L,2", max_err, left_tangent: "2," = None, right_tangent: "2," = None):
    """fit one (ore more) Bezier curves to a set of points"""
    
    weights: "N" = (lambda x,n: (float(x)**-np.arange(1,n+1)) / (1 - float(x)**-n) * (x-1))(2, min(10, len(points)-2))
    
    if left_tangent is None:
        # points[1] - points[0]
        l_vecs: "N,2" = points[2:2+len(weights)] - points[1]
        left_tangent = normalize(np.einsum('np,n->p', l_vecs, weights))
        
    if right_tangent is None:
        # points[-2] - points[-1]
        r_vecs: "N,2" = points[-3:-3-len(weights):-1] - points[-2]
        right_tangent = normalize(np.einsum('np,n->p', r_vecs, weights))
    
    # use heuristic if region only has two points in it
    if len(points) == 2:
        dist = np.linalg.norm(points[0] - points[1]) / 3.0
        return [[
            points[0],
            points[0] + left_tangent * dist,
            points[1] + right_tangent * dist,
            points[1],
        ]]
    
    u = None
    for _ in range(32):
        if u is None:
            # parameterize points
            u = [0]
            u[1:] = np.cumsum(np.linalg.norm(points[1:] - points[:-1], axis=1))
            u /= u[-1]
        else:
            # iterate parameterization
            u = newton_raphson_root_find(bez_curve, points, u)
            
        bez_curve = generate_bezier(points, u, left_tangent, right_tangent)
        
        # compute error
        errs = ((q(bez_curve, u) - points) ** 2).sum(-1)
        split_point = errs.argmax()
        err = errs[split_point]
            
        if err < max_err:
            return [bez_curve]
        
        if err > max_err ** 2:
            # error too large
            break

    # Fitting failed -- split at max error point and fit recursively
    center_tangent = normalize(points[split_point-1] - points[split_point+1])
    return [
        *fit_bezier(points[:split_point+1], max_err, left_tangent, center_tangent),
        *fit_bezier(points[split_point:], max_err, -center_tangent, right_tangent),
    ]

def generate_bezier(points: "L,2", u: "L,", left_tangent: "2,", right_tangent: "2,"):
    bez_curve: "4,2" = np.array([points[0], points[0], points[-1], points[-1]])

    # compute the A's
    A = (3 * (1-u) * u * np.array([1-u,u])).T[..., None] * np.array([left_tangent, right_tangent])
    
    # Create the C and X matrices
    C = np.einsum('lix,ljx->ij', A, A)
    X = np.einsum('lix,lx->i', A, points - q(bez_curve, u))

    # Compute the determinants of C and X
    det_C0_C1 = C[0][0] * C[1][1] - C[1][0] * C[0][1]
    det_C0_X  = C[0][0] * X[1]    - C[1][0] * X[0]
    det_X_C1  = X[0]    * C[1][1] - X[1]    * C[0][1]

    # Finally, derive alpha values
    alpha_l = 0.0 if abs(det_C0_C1) < 1e-5 else det_X_C1 / det_C0_C1
    alpha_r = 0.0 if abs(det_C0_C1) < 1e-5 else det_C0_X / det_C0_C1

    # If alpha negative, use the Wu/Barsky heuristic (see text)
    # (if alpha is 0, you get coincident control points that lead to
    # divide by zero in any subsequent NewtonRaphsonRootFind() call)
    seg_len = np.linalg.norm(points[0] - points[-1])
    epsilon = 1e-6 * seg_len
    if alpha_l < epsilon or alpha_r < epsilon:
        # fall back on standard (probably inaccurate) formula, and subdivide further if needed.
        bez_curve[1] += left_tangent * (seg_len / 3.0)
        bez_curve[2] += right_tangent * (seg_len / 3.0)

    else:
        # First and last control points of the Bezier curve are
        # positioned exactly at the first and last data points
        # Control points 1 and 2 are positioned an alpha distance out
        # on the tangent vectors, left and right, respectively
        bez_curve[1] += left_tangent * alpha_l
        bez_curve[2] += right_tangent * alpha_r

    return bez_curve


def newton_raphson_root_find(bez: "4,2", points: "L,2", u: "L,"):
    """
    Newton's root finding algorithm calculates f(x)=0 by reiterating
    x_n+1 = x_n - f(x_n)/f'(x_n)
    We are trying to find curve parameter u for some point p that minimizes
    the distance from that point to the curve. Distance point to curve is d=q(u)-p.
    At minimum distance the point is perpendicular to the curve.
    We are solving
    f = q(u)-p * q'(u) = 0
    with
    f' = q'(u) * q'(u) + q(u)-p * q''(u)
    gives
    u_n+1 = u_n - |q(u_n)-p * q'(u_n)| / |q'(u_n)**2 + q(u_n)-p * q''(u_n)|
    """
    
    d = q(bez, u) - points
    qp = qprime(bez, u)
    num = (d * qp).sum(-1)
    den = (qp**2 + d*qprimeprime(bez, u)).sum(-1)
    
    return u + np.where(den==0, 0, num/den)
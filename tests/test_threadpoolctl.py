import os
import re
import pytest

from threadpoolctl import threadpool_limits, _ThreadpoolInfo
from threadpoolctl import _ALL_PREFIXES, _ALL_USER_APIS

from .utils import cython_extensions_compiled
from .utils import libopenblas_paths
from .utils import scipy


def is_old_openblas(module):
    # Possible bug in getting maximum number of threads with OpenBLAS < 0.2.16
    # and OpenBLAS does not expose its version before 0.3.4.
    return module.internal_api == "openblas" and module.version is None


def effective_num_threads(nthreads, max_threads):
    if nthreads is None or nthreads > max_threads:
        return max_threads
    return nthreads


def _threadpool_info():
    # Like threadpool_info but return the object instead of the list of dicts
    return _ThreadpoolInfo(user_api=_ALL_USER_APIS)


@pytest.mark.parametrize("prefix", _ALL_PREFIXES)
@pytest.mark.parametrize("limit", [1, 3])
def test_threadpool_limits_by_prefix(prefix, limit):
    # Check that the maximum number of threads can be set by prefix
    original_infos = _threadpool_info()

    modules_matching_prefix = original_infos.get_modules("prefix", prefix)
    if not modules_matching_prefix:
        pytest.skip("Requires {} runtime".format(prefix))

    with threadpool_limits(limits={prefix: limit}):
        for module in modules_matching_prefix:
            if is_old_openblas(module):
                continue
            # threadpool_limits only sets an upper bound on the number of
            # threads.
            assert 0 < module.get_num_threads() <= limit
    assert _threadpool_info() == original_infos


@pytest.mark.parametrize("user_api", (None, "blas", "openmp"))
@pytest.mark.parametrize("limit", [1, 3])
def test_set_threadpool_limits_by_api(user_api, limit):
    # Check that the maximum number of threads can be set by user_api
    original_infos = _threadpool_info()

    modules_matching_api = original_infos.get_modules("user_api", user_api)
    if not modules_matching_api:
        user_apis = _ALL_USER_APIS if user_api is None else [user_api]
        pytest.skip("Requires a library which api is in {}".format(user_apis))

    with threadpool_limits(limits=limit, user_api=user_api):
        for module in modules_matching_api:
            if is_old_openblas(module):
                continue
            # threadpool_limits only sets an upper bound on the number of
            # threads.
            assert 0 < module.get_num_threads() <= limit

    assert _threadpool_info() == original_infos


def test_threadpool_limits_function_with_side_effect():
    # Check that threadpool_limits can be used as a function with
    # side effects instead of a context manager.
    original_infos = _threadpool_info()

    threadpool_limits(limits=1)
    try:
        for module in _threadpool_info():
            if is_old_openblas(module):
                continue
            assert module.num_threads == 1
    finally:
        # Restore the original limits so that this test does not have any
        # side-effect.
        threadpool_limits(limits=original_infos)

    assert _threadpool_info() == original_infos


def test_set_threadpool_limits_no_limit():
    # Check that limits=None does nothing.
    original_infos = _threadpool_info()
    with threadpool_limits(limits=None):
        assert _threadpool_info() == original_infos

    assert _threadpool_info() == original_infos


def test_threadpool_limits_manual_unregister():
    # Check that threadpool_limits can be used as an object which holds the
    # original state of the threadpools and that can be restored thanks to the
    # dedicated unregister method
    original_infos = _threadpool_info()

    limits = threadpool_limits(limits=1)
    try:
        for module in _threadpool_info():
            if is_old_openblas(module):
                continue
            assert module.num_threads == 1
    finally:
        # Restore the original limits so that this test does not have any
        # side-effect.
        limits.unregister()

    assert _threadpool_info() == original_infos


def test_threadpool_limits_bad_input():
    # Check that appropriate errors are raised for invalid arguments
    match = re.escape("user_api must be either in {} or None."
                      .format(_ALL_USER_APIS))
    with pytest.raises(ValueError, match=match):
        threadpool_limits(limits=1, user_api="wrong")

    with pytest.raises(TypeError,
                       match="limits must either be an int, a list or a dict"):
        threadpool_limits(limits=(1, 2, 3))


@pytest.mark.skipif(not cython_extensions_compiled,
                    reason='Requires cython extensions to be compiled')
@pytest.mark.parametrize('num_threads', [1, 2, 4])
def test_openmp_limit_num_threads(num_threads):
    # checks that OpenMP effectively uses the number of threads requested by
    # the context manager
    from ._openmp_test_helper import check_openmp_num_threads

    old_num_threads = check_openmp_num_threads(100)

    with threadpool_limits(limits=num_threads):
        assert check_openmp_num_threads(100) in (num_threads, old_num_threads)
    assert check_openmp_num_threads(100) == old_num_threads


@pytest.mark.skipif(not cython_extensions_compiled,
                    reason='Requires cython extensions to be compiled')
@pytest.mark.parametrize('nthreads_outer', [None, 1, 2, 4])
def test_openmp_nesting(nthreads_outer):
    # checks that OpenMP effectively uses the number of threads requested by
    # the context manager when nested in an outer OpenMP loop.
    from ._openmp_test_helper import check_nested_openmp_loops
    from ._openmp_test_helper import get_inner_compiler
    from ._openmp_test_helper import get_outer_compiler

    inner_cc = get_inner_compiler()
    outer_cc = get_outer_compiler()

    outer_num_threads, inner_num_threads = check_nested_openmp_loops(10)

    original_infos = _threadpool_info()
    openmp_infos = original_infos.get_modules("user_api", "openmp")

    if "gcc" in (inner_cc, outer_cc):
        assert original_infos.get_modules("prefix", "libgomp")

    if "clang" in (inner_cc, outer_cc):
        assert original_infos.get_modules("prefix", "libomp")

    if inner_cc == outer_cc:
        # The openmp runtime should be shared by default, meaning that
        # the inner loop should automatically be run serially by the OpenMP
        # runtime.
        assert inner_num_threads == 1
    else:
        # There should be at least 2 OpenMP runtime detected.
        assert len(openmp_infos) >= 2

    with threadpool_limits(limits=1) as threadpoolctx:
        max_threads = threadpoolctx.get_original_num_threads()["openmp"]
        nthreads = effective_num_threads(nthreads_outer, max_threads)

        # Ask outer loop to run on nthreads threads and inner loop run on 1
        # thread
        outer_num_threads, inner_num_threads = \
            check_nested_openmp_loops(10, nthreads)

    # The state of the original state of all threadpools should have been
    # restored.
    assert _threadpool_info() == original_infos

    # The number of threads available in the outer loop should not have been
    # decreased:
    assert outer_num_threads == nthreads

    # The number of threads available in the inner loop should have been
    # set to 1 so avoid oversubscription and preserve performance:
    if inner_cc != outer_cc:
        if inner_num_threads != 1:
            # XXX: this does not always work when nesting independent openmp
            # implementations. See: https://github.com/jeremiedbb/Nested_OpenMP
            pytest.xfail("Inner OpenMP num threads was %d instead of 1"
                         % inner_num_threads)
    assert inner_num_threads == 1


def test_shipped_openblas():
    # checks that OpenBLAS effectively uses the number of threads requested by
    # the context manager
    original_info = _threadpool_info()

    openblas_modules = original_info.get_modules("internal_api", "openblas")

    with threadpool_limits(1):
        for module in openblas_modules:
            assert module.get_num_threads() == 1

    assert original_info == _threadpool_info()


@pytest.mark.skipif(len(libopenblas_paths) < 2,
                    reason="need at least 2 shipped openblas library")
def test_multiple_shipped_openblas():
    # This redundant test is meant to make it easier to see if the system
    # has 2 or more active openblas runtimes available just be reading the
    # pytest report (whether or not this test has been skipped).
    test_shipped_openblas()


@pytest.mark.skipif(scipy is None, reason="requires scipy")
@pytest.mark.parametrize("nthreads_outer", [None, 1, 2, 4])
def test_nested_prange_blas(nthreads_outer):
    # Check that the BLAS linked to scipy effectively uses the number of
    # threads requested by the context manager when nested in an outer OpenMP
    # loop.
    import numpy as np
    from ._openmp_test_helper import check_nested_prange_blas

    original_info = _threadpool_info()

    blas_info = original_info.get_modules("user_api", "blas")
    blis_info = original_info.get_modules("internal_api", "blis")

    # skip if the BLAS used by numpy is an old openblas. OpenBLAS 0.3.3 and
    # older are known to cause an unrecoverable deadlock at process shutdown
    # time (after pytest has exited).
    # numpy can be linked to BLIS for CBLAS and OpenBLAS for LAPACK. In that
    # case this test will run BLIS gemm so no need to skip.
    if not blis_info and any(is_old_openblas(module) for module in blas_info):
        pytest.skip("Old OpenBLAS: skipping test to avoid deadlock")

    A = np.ones((1000, 10))
    B = np.ones((100, 10))

    with threadpool_limits(limits=1) as threadpoolctx:
        max_threads = threadpoolctx.get_original_num_threads()["openmp"]
        nthreads = effective_num_threads(nthreads_outer, max_threads)

        result = check_nested_prange_blas(A, B, nthreads)
        C, prange_num_threads, threadpool_infos = result

    assert np.allclose(C, np.dot(A, B.T))
    assert prange_num_threads == nthreads

    nested_blas_info = threadpool_infos.get_modules("user_api", "blas")
    assert len(nested_blas_info) == len(blas_info)
    for module in nested_blas_info:
        assert module.num_threads == 1

    assert original_info == _threadpool_info()


@pytest.mark.parametrize("limit", [1, None])
def test_get_original_num_threads(limit):
    # Tests the method get_original_num_threads of the context manager
    with threadpool_limits(limits=2, user_api="blas") as ctl:
        # set different blas num threads to start with (when multiple openblas)
        if ctl._original_info:
            ctl._original_info.modules[0].set_num_threads(1)

        original_infos = _threadpool_info()
        with threadpool_limits(limits=limit, user_api="blas") as threadpoolctx:
            original_num_threads = threadpoolctx.get_original_num_threads()
            print(original_num_threads)

            assert "openmp" not in original_num_threads

            blas_infos = original_infos.get_modules("user_api", "blas")
            if blas_infos:
                expected = min(module.num_threads for module in blas_infos)
                assert original_num_threads["blas"] == expected
            else:
                assert original_num_threads["blas"] is None

            if len(libopenblas_paths) >= 2:
                with pytest.warns(None, match="Multiple value possible"):
                    threadpoolctx.get_original_num_threads()


def test_mkl_threading_layer():
    # Check that threadpool_info correctly recovers the threading layer used
    # by mkl
    mkl_info = _threadpool_info().get_modules("internal_api", "mkl")
    expected_layer = os.getenv("MKL_THREADING_LAYER")

    if not (mkl_info and expected_layer):
        pytest.skip("requires MKL and the environment variable "
                    "MKL_THREADING_LAYER set")

    actual_layer = mkl_info.modules[0].threading_layer
    assert actual_layer == expected_layer.lower()

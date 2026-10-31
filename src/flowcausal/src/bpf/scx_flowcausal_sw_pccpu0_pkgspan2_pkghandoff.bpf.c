// SPDX-License-Identifier: GPL-2.0
/*
 * FlowCausal-StageWidth-PCpu0-PackageSpan2-PackageHandoff.
 *
 * Candidate:
 *   - keep the current best package handoff mechanism;
 *   - keep C++ BUILD on CPU0-2 and Python TEST on CPU3-7;
 *   - before the C++ root exits, let the package session use a narrow
 *     two-CPU base span starting at CPU5 instead of a single CPU5 anchor;
 *   - after the C++ root exits, package BUILD_WIDE is still handed to CPU0-2.
 */
#define FLOWCAUSAL_OPS_NAME "flowcausal_sw_pccpu0_pkgspan2_pkghandoff"
#define FLOWCAUSAL_DEFAULT_MODE_AWARE true
#define FLOWCAUSAL_DEFAULT_BPF_AUTO_FRONTIER true
#define FLOWCAUSAL_DEFAULT_CONTROL_EVENT_MASK (1U << FLOW_EVT_EXEC)
#define FLOWCAUSAL_DEFAULT_RESPECT_C_ASSIST true
#define FLOWCAUSAL_DEFAULT_CONVERGE false
#define FLOWCAUSAL_DEFAULT_SESSION_FRONTIER_DSQ false
#define FLOWCAUSAL_DEFAULT_ADAPTIVE_WIDTH true
#define FLOWCAUSAL_DEFAULT_DEADLINE_VTIME false
#define FLOWCAUSAL_DEFAULT_CONVERGE_TAIL_DSQ false
#define FLOWCAUSAL_DEFAULT_FRONTIER_LOCAL_DSQ true
#define FLOWCAUSAL_DEFAULT_PACKAGE_BASE_SPAN 2U
#define FLOWCAUSAL_DEFAULT_PACKAGE_RESCUE_AFTER_CXX_ROOT true
#define FLOWCAUSAL_DEFAULT_PACKAGE_RESCUE_NS 1000000000ULL
#define FLOWCAUSAL_DEFAULT_PACKAGE_RESCUE_CXX_CPUSET true
#define FLOWCAUSAL_DEFAULT_PARENT_EXIT_LATENCY true
#define FLOWCAUSAL_DEFAULT_PARENT_EXIT_LATENCY_SLICE_NS 0ULL
#define FLOWCAUSAL_DEFAULT_PARENT_EXIT_CLEANUP true
#define FLOWCAUSAL_DEFAULT_PARENT_EXIT_CLEANUP_NS 1000000000ULL
#define FLOWCAUSAL_DEFAULT_PARENT_EXIT_CLEANUP_CPU 0
#define FLOWCAUSAL_DEFAULT_PARENT_EXIT_CLEANUP_CXX_ONLY true

#define flowcausal_ops flowcausal_sw_pccpu0_pkgspan2_pkghandoff_ops
#define flowcausal_select_cpu flowcausal_sw_pccpu0_pkgspan2_pkghandoff_select_cpu
#define flowcausal_enqueue flowcausal_sw_pccpu0_pkgspan2_pkghandoff_enqueue
#define flowcausal_dispatch flowcausal_sw_pccpu0_pkgspan2_pkghandoff_dispatch
#define flowcausal_running flowcausal_sw_pccpu0_pkgspan2_pkghandoff_running
#define flowcausal_stopping flowcausal_sw_pccpu0_pkgspan2_pkghandoff_stopping
#define flowcausal_enable flowcausal_sw_pccpu0_pkgspan2_pkghandoff_enable
#define flowcausal_init flowcausal_sw_pccpu0_pkgspan2_pkghandoff_init
#define flowcausal_exit flowcausal_sw_pccpu0_pkgspan2_pkghandoff_exit
#define flowcausal_on_fork flowcausal_sw_pccpu0_pkgspan2_pkghandoff_on_fork
#define flowcausal_on_exec flowcausal_sw_pccpu0_pkgspan2_pkghandoff_on_exec
#define flowcausal_on_exit flowcausal_sw_pccpu0_pkgspan2_pkghandoff_on_exit
#define flowcausal_on_free flowcausal_sw_pccpu0_pkgspan2_pkghandoff_on_free
#define flowcausal_on_wakeup flowcausal_sw_pccpu0_pkgspan2_pkghandoff_on_wakeup
#define flowcausal_on_wakeup_new flowcausal_sw_pccpu0_pkgspan2_pkghandoff_on_wakeup_new

#include "scx_flowcausal.bpf.c"

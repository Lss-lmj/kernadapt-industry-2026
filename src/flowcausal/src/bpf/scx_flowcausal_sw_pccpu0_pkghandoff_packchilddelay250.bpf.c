// SPDX-License-Identifier: GPL-2.0
/*
 * FlowCausal-StageWidth-PCpu0-PackageHandoff-PackChildDelay250.
 *
 * Candidate:
 *   - keep the current best package handoff after the C++ root exits;
 *   - keep the CPU0 parent cleanup lane;
 *   - keep package root wrapper on the current best frontier/locality path;
 *   - during the first 250 ms after scheduler attach, suppress only package
 *     non-root BUILD_WIDE frontier admission and steer those tasks to CPU7.
 *
 * This is the conservative follow-up to packchilddelay500: preserve its
 * package-nonstage benefit while reducing damage to true package stages.
 */
#define FLOWCAUSAL_OPS_NAME "flowcausal_sw_pccpu0_pkghandoff_packchilddelay250"
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
#define FLOWCAUSAL_DEFAULT_PACKAGE_RESCUE_AFTER_CXX_ROOT true
#define FLOWCAUSAL_DEFAULT_PACKAGE_RESCUE_NS 1000000000ULL
#define FLOWCAUSAL_DEFAULT_PACKAGE_RESCUE_CXX_CPUSET true
#define FLOWCAUSAL_DEFAULT_PARENT_EXIT_LATENCY true
#define FLOWCAUSAL_DEFAULT_PARENT_EXIT_LATENCY_SLICE_NS 0ULL
#define FLOWCAUSAL_DEFAULT_PARENT_EXIT_CLEANUP true
#define FLOWCAUSAL_DEFAULT_PARENT_EXIT_CLEANUP_NS 1000000000ULL
#define FLOWCAUSAL_DEFAULT_PARENT_EXIT_CLEANUP_CPU 0
#define FLOWCAUSAL_DEFAULT_PARENT_EXIT_CLEANUP_CXX_ONLY true
#define FLOWCAUSAL_DEFAULT_CXX_FIRST_WINDOW_NS 250000000ULL
#define FLOWCAUSAL_DEFAULT_CXX_FIRST_SUPPRESS_CPU 7
#define FLOWCAUSAL_DEFAULT_CXX_FIRST_SUPPRESS_PACKAGE_ONLY true
#define FLOWCAUSAL_DEFAULT_CXX_FIRST_SUPPRESS_NONROOT_ONLY true

#define flowcausal_ops flowcausal_sw_pccpu0_pkghandoff_packchilddelay250_ops
#define flowcausal_select_cpu flowcausal_sw_pccpu0_pkghandoff_packchilddelay250_select_cpu
#define flowcausal_enqueue flowcausal_sw_pccpu0_pkghandoff_packchilddelay250_enqueue
#define flowcausal_dispatch flowcausal_sw_pccpu0_pkghandoff_packchilddelay250_dispatch
#define flowcausal_running flowcausal_sw_pccpu0_pkghandoff_packchilddelay250_running
#define flowcausal_stopping flowcausal_sw_pccpu0_pkghandoff_packchilddelay250_stopping
#define flowcausal_enable flowcausal_sw_pccpu0_pkghandoff_packchilddelay250_enable
#define flowcausal_init flowcausal_sw_pccpu0_pkghandoff_packchilddelay250_init
#define flowcausal_exit flowcausal_sw_pccpu0_pkghandoff_packchilddelay250_exit
#define flowcausal_on_fork flowcausal_sw_pccpu0_pkghandoff_packchilddelay250_on_fork
#define flowcausal_on_exec flowcausal_sw_pccpu0_pkghandoff_packchilddelay250_on_exec
#define flowcausal_on_exit flowcausal_sw_pccpu0_pkghandoff_packchilddelay250_on_exit
#define flowcausal_on_free flowcausal_sw_pccpu0_pkghandoff_packchilddelay250_on_free
#define flowcausal_on_wakeup flowcausal_sw_pccpu0_pkghandoff_packchilddelay250_on_wakeup
#define flowcausal_on_wakeup_new flowcausal_sw_pccpu0_pkghandoff_packchilddelay250_on_wakeup_new

#include "scx_flowcausal.bpf.c"

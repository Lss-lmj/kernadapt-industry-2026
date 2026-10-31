// SPDX-License-Identifier: GPL-2.0
/*
 * FlowCausal-StageWidth-PCpu0-PackUnbudget.
 *
 * Latest candidate:
 *   1. keep the current best stage-width policy;
 *   2. only a C++/build root exit opens the parent cleanup window;
 *   3. run that parent cleanup continuation on CPU0 for locality;
 *   4. let package/build-wide roots bypass the frontier boost budget.
 *
 * The short file/symbol name avoids bpftool skeleton-name truncation.
 */
#define FLOWCAUSAL_OPS_NAME "flowcausal_sw_pccpu0_packub"
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
#define FLOWCAUSAL_DEFAULT_PACKAGE_UNBUDGET true
#define FLOWCAUSAL_DEFAULT_PARENT_EXIT_LATENCY true
#define FLOWCAUSAL_DEFAULT_PARENT_EXIT_LATENCY_SLICE_NS 0ULL
#define FLOWCAUSAL_DEFAULT_PARENT_EXIT_CLEANUP true
#define FLOWCAUSAL_DEFAULT_PARENT_EXIT_CLEANUP_NS 1000000000ULL
#define FLOWCAUSAL_DEFAULT_PARENT_EXIT_CLEANUP_CPU 0
#define FLOWCAUSAL_DEFAULT_PARENT_EXIT_CLEANUP_CXX_ONLY true

#define flowcausal_ops flowcausal_sw_pccpu0_packub_ops
#define flowcausal_select_cpu flowcausal_sw_pccpu0_packub_select_cpu
#define flowcausal_enqueue flowcausal_sw_pccpu0_packub_enqueue
#define flowcausal_dispatch flowcausal_sw_pccpu0_packub_dispatch
#define flowcausal_running flowcausal_sw_pccpu0_packub_running
#define flowcausal_stopping flowcausal_sw_pccpu0_packub_stopping
#define flowcausal_enable flowcausal_sw_pccpu0_packub_enable
#define flowcausal_init flowcausal_sw_pccpu0_packub_init
#define flowcausal_exit flowcausal_sw_pccpu0_packub_exit
#define flowcausal_on_fork flowcausal_sw_pccpu0_packub_on_fork
#define flowcausal_on_exec flowcausal_sw_pccpu0_packub_on_exec
#define flowcausal_on_exit flowcausal_sw_pccpu0_packub_on_exit
#define flowcausal_on_free flowcausal_sw_pccpu0_packub_on_free
#define flowcausal_on_wakeup flowcausal_sw_pccpu0_packub_on_wakeup
#define flowcausal_on_wakeup_new flowcausal_sw_pccpu0_packub_on_wakeup_new

#include "scx_flowcausal.bpf.c"

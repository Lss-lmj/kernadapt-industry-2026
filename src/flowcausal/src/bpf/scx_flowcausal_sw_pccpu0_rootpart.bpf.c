// SPDX-License-Identifier: GPL-2.0
/*
 * FlowCausal-StageWidth-PCpu0-RootPartition.
 *
 * Latest candidate:
 *   1. keep the current best cxx-only parent cleanup on CPU0;
 *   2. mark harness root wrappers explicitly;
 *   3. narrow only the Python root wrapper preparation/wait path to CPU3-4;
 *   4. leave real child BUILD/TEST/package commands on the existing policy.
 */
#define FLOWCAUSAL_OPS_NAME "flowcausal_sw_pccpu0_rootpart"
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
#define FLOWCAUSAL_DEFAULT_ROOT_PREP_PARTITION true
#define FLOWCAUSAL_DEFAULT_PARENT_EXIT_LATENCY true
#define FLOWCAUSAL_DEFAULT_PARENT_EXIT_LATENCY_SLICE_NS 0ULL
#define FLOWCAUSAL_DEFAULT_PARENT_EXIT_CLEANUP true
#define FLOWCAUSAL_DEFAULT_PARENT_EXIT_CLEANUP_NS 1000000000ULL
#define FLOWCAUSAL_DEFAULT_PARENT_EXIT_CLEANUP_CPU 0
#define FLOWCAUSAL_DEFAULT_PARENT_EXIT_CLEANUP_CXX_ONLY true

#define flowcausal_ops flowcausal_sw_pccpu0_rootpart_ops
#define flowcausal_select_cpu flowcausal_sw_pccpu0_rootpart_select_cpu
#define flowcausal_enqueue flowcausal_sw_pccpu0_rootpart_enqueue
#define flowcausal_dispatch flowcausal_sw_pccpu0_rootpart_dispatch
#define flowcausal_running flowcausal_sw_pccpu0_rootpart_running
#define flowcausal_stopping flowcausal_sw_pccpu0_rootpart_stopping
#define flowcausal_enable flowcausal_sw_pccpu0_rootpart_enable
#define flowcausal_init flowcausal_sw_pccpu0_rootpart_init
#define flowcausal_exit flowcausal_sw_pccpu0_rootpart_exit
#define flowcausal_on_fork flowcausal_sw_pccpu0_rootpart_on_fork
#define flowcausal_on_exec flowcausal_sw_pccpu0_rootpart_on_exec
#define flowcausal_on_exit flowcausal_sw_pccpu0_rootpart_on_exit
#define flowcausal_on_free flowcausal_sw_pccpu0_rootpart_on_free
#define flowcausal_on_wakeup flowcausal_sw_pccpu0_rootpart_on_wakeup
#define flowcausal_on_wakeup_new flowcausal_sw_pccpu0_rootpart_on_wakeup_new

#include "scx_flowcausal.bpf.c"

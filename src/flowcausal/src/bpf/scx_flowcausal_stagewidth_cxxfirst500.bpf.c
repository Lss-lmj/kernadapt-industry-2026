// SPDX-License-Identifier: GPL-2.0
/*
 * FlowCausal-StageWidth-CxxFirst500.
 *
 * This variant keeps StageWidth/static-root scheduling and adds a short
 * cxx-first startup window.  For the first 500 ms after scheduler attach,
 * non-cxx frontier tasks are downgraded to the shared path and co-located on
 * CPU0, while the cxx BUILD session keeps its normal CPU0-2 frontier path.
 */
#define FLOWCAUSAL_OPS_NAME "flowcausal_stagewidth_cxxfirst500"
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
#define FLOWCAUSAL_DEFAULT_CXX_FIRST_WINDOW_NS 500000000ULL

#define flowcausal_ops flowcausal_stagewidth_cxxfirst500_ops
#define flowcausal_select_cpu flowcausal_stagewidth_cxxfirst500_select_cpu
#define flowcausal_enqueue flowcausal_stagewidth_cxxfirst500_enqueue
#define flowcausal_dispatch flowcausal_stagewidth_cxxfirst500_dispatch
#define flowcausal_running flowcausal_stagewidth_cxxfirst500_running
#define flowcausal_stopping flowcausal_stagewidth_cxxfirst500_stopping
#define flowcausal_enable flowcausal_stagewidth_cxxfirst500_enable
#define flowcausal_init flowcausal_stagewidth_cxxfirst500_init
#define flowcausal_exit flowcausal_stagewidth_cxxfirst500_exit
#define flowcausal_on_fork flowcausal_stagewidth_cxxfirst500_on_fork
#define flowcausal_on_exec flowcausal_stagewidth_cxxfirst500_on_exec
#define flowcausal_on_exit flowcausal_stagewidth_cxxfirst500_on_exit
#define flowcausal_on_free flowcausal_stagewidth_cxxfirst500_on_free
#define flowcausal_on_wakeup flowcausal_stagewidth_cxxfirst500_on_wakeup
#define flowcausal_on_wakeup_new flowcausal_stagewidth_cxxfirst500_on_wakeup_new

#include "scx_flowcausal.bpf.c"

// SPDX-License-Identifier: GPL-2.0
/*
 * FlowCausal-StageWidth-ParentLatency.
 *
 * Keep the StageWidth/static-root data path, but replace parent-exit frontier
 * boosting with a one-shot latency continuation for the harness supervisor.
 * The parent gets a short low-latency dispatch after a tracked child exits,
 * then its temporary context is removed so later fork/exec activity is not
 * inherited as a FlowCausal workflow.
 */
#define FLOWCAUSAL_OPS_NAME "flowcausal_stagewidth_parentlatency"
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
#define FLOWCAUSAL_DEFAULT_PARENT_EXIT_LATENCY true
#define FLOWCAUSAL_DEFAULT_PARENT_EXIT_LATENCY_SLICE_NS 1000000ULL

#define flowcausal_ops flowcausal_stagewidth_parentlatency_ops
#define flowcausal_select_cpu flowcausal_stagewidth_parentlatency_select_cpu
#define flowcausal_enqueue flowcausal_stagewidth_parentlatency_enqueue
#define flowcausal_dispatch flowcausal_stagewidth_parentlatency_dispatch
#define flowcausal_running flowcausal_stagewidth_parentlatency_running
#define flowcausal_stopping flowcausal_stagewidth_parentlatency_stopping
#define flowcausal_enable flowcausal_stagewidth_parentlatency_enable
#define flowcausal_init flowcausal_stagewidth_parentlatency_init
#define flowcausal_exit flowcausal_stagewidth_parentlatency_exit
#define flowcausal_on_fork flowcausal_stagewidth_parentlatency_on_fork
#define flowcausal_on_exec flowcausal_stagewidth_parentlatency_on_exec
#define flowcausal_on_exit flowcausal_stagewidth_parentlatency_on_exit
#define flowcausal_on_free flowcausal_stagewidth_parentlatency_on_free
#define flowcausal_on_wakeup flowcausal_stagewidth_parentlatency_on_wakeup
#define flowcausal_on_wakeup_new flowcausal_stagewidth_parentlatency_on_wakeup_new

#include "scx_flowcausal.bpf.c"

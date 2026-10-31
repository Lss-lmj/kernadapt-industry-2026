// SPDX-License-Identifier: GPL-2.0
/*
 * FlowCausal-StageWidth-ChainBoost.
 *
 * This variant keeps the current StageWidth/static-root mechanism and adds a
 * narrow compile-chain boost for cxx build-heavy harness phases.  The boost is
 * deliberately limited to compiler/linker children under BUILD_WIDE sessions
 * anchored at CPU0, so repository copy, Python test tail, and package phases
 * keep the proven StageWidth behavior.
 */
#define FLOWCAUSAL_OPS_NAME "flowcausal_stagewidth_chainboost"
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
#define FLOWCAUSAL_DEFAULT_COMPILE_CHAIN_BOOST true
#define FLOWCAUSAL_DEFAULT_COMPILE_CHAIN_BOOST_NS 1500000ULL

#define flowcausal_ops flowcausal_stagewidth_chainboost_ops
#define flowcausal_select_cpu flowcausal_stagewidth_chainboost_select_cpu
#define flowcausal_enqueue flowcausal_stagewidth_chainboost_enqueue
#define flowcausal_dispatch flowcausal_stagewidth_chainboost_dispatch
#define flowcausal_running flowcausal_stagewidth_chainboost_running
#define flowcausal_stopping flowcausal_stagewidth_chainboost_stopping
#define flowcausal_enable flowcausal_stagewidth_chainboost_enable
#define flowcausal_init flowcausal_stagewidth_chainboost_init
#define flowcausal_exit flowcausal_stagewidth_chainboost_exit
#define flowcausal_on_fork flowcausal_stagewidth_chainboost_on_fork
#define flowcausal_on_exec flowcausal_stagewidth_chainboost_on_exec
#define flowcausal_on_exit flowcausal_stagewidth_chainboost_on_exit
#define flowcausal_on_free flowcausal_stagewidth_chainboost_on_free
#define flowcausal_on_wakeup flowcausal_stagewidth_chainboost_on_wakeup
#define flowcausal_on_wakeup_new flowcausal_stagewidth_chainboost_on_wakeup_new

#include "scx_flowcausal.bpf.c"

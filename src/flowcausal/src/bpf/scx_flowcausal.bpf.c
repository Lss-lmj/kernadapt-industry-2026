/* SPDX-License-Identifier: GPL-2.0 */
/*
 * scx_flowcausal.bpf.c
 *
 * A conservative sched_ext prototype for FlowCausal.  User space identifies a
 * session--stage--cohort critical frontier and writes bounded hints into
 * BPF maps.  The kernel scheduler consumes only those hints, applies a small
 * virtual-time bias or locality hinting, and falls back to ordinary shared
 * scheduling when the hint is missing, expired, or outside its budget.
 */
#include <scx/common.bpf.h>
#include <bpf/bpf_helpers.h>
#include <bpf/bpf_tracing.h>
#include <flowcausal.h>

char _license[] SEC("license") = "GPL";

#ifndef FLOWCAUSAL_OPS_NAME
#define FLOWCAUSAL_OPS_NAME "flowcausal"
#endif

#ifndef FLOWCAUSAL_DEFAULT_MODE_AWARE
#define FLOWCAUSAL_DEFAULT_MODE_AWARE false
#endif

#ifndef FLOWCAUSAL_DEFAULT_BPF_AUTO_FRONTIER
#define FLOWCAUSAL_DEFAULT_BPF_AUTO_FRONTIER false
#endif

#ifndef FLOWCAUSAL_DEFAULT_CONTROL_EVENT_MASK
#define FLOWCAUSAL_DEFAULT_CONTROL_EVENT_MASK 0U
#endif

#ifndef FLOWCAUSAL_DEFAULT_RESPECT_C_ASSIST
#define FLOWCAUSAL_DEFAULT_RESPECT_C_ASSIST false
#endif

#ifndef FLOWCAUSAL_DEFAULT_CONVERGE
#define FLOWCAUSAL_DEFAULT_CONVERGE false
#endif

#ifndef FLOWCAUSAL_DEFAULT_SESSION_FRONTIER_DSQ
#define FLOWCAUSAL_DEFAULT_SESSION_FRONTIER_DSQ false
#endif

#ifndef FLOWCAUSAL_DEFAULT_ADAPTIVE_WIDTH
#define FLOWCAUSAL_DEFAULT_ADAPTIVE_WIDTH false
#endif

#ifndef FLOWCAUSAL_DEFAULT_DEADLINE_VTIME
#define FLOWCAUSAL_DEFAULT_DEADLINE_VTIME false
#endif

#ifndef FLOWCAUSAL_DEFAULT_CONVERGE_TAIL_DSQ
#define FLOWCAUSAL_DEFAULT_CONVERGE_TAIL_DSQ false
#endif

#ifndef FLOWCAUSAL_DEFAULT_CRITICAL_PREFIX_DSQ
#define FLOWCAUSAL_DEFAULT_CRITICAL_PREFIX_DSQ false
#endif

#ifndef FLOWCAUSAL_DEFAULT_CRITICAL_PREFIX_INCLUDE_PACKAGE
#define FLOWCAUSAL_DEFAULT_CRITICAL_PREFIX_INCLUDE_PACKAGE false
#endif

#ifndef FLOWCAUSAL_DEFAULT_PY_TEST_REMEMBERED_FIRST
#define FLOWCAUSAL_DEFAULT_PY_TEST_REMEMBERED_FIRST false
#endif

#ifndef FLOWCAUSAL_DEFAULT_PY_TEST_IDLE_PICK
#define FLOWCAUSAL_DEFAULT_PY_TEST_IDLE_PICK false
#endif

#ifndef FLOWCAUSAL_DEFAULT_PY_TEST_NO_FRONTIER
#define FLOWCAUSAL_DEFAULT_PY_TEST_NO_FRONTIER false
#endif

#ifndef FLOWCAUSAL_DEFAULT_PY_TEST_BURST_NS
#define FLOWCAUSAL_DEFAULT_PY_TEST_BURST_NS 0ULL
#endif

#ifndef FLOWCAUSAL_DEFAULT_PY_TEST_BURST_ANCHOR_CPU
#define FLOWCAUSAL_DEFAULT_PY_TEST_BURST_ANCHOR_CPU 0
#endif

#ifndef FLOWCAUSAL_DEFAULT_PY_TEST_BURST_SPAN
#define FLOWCAUSAL_DEFAULT_PY_TEST_BURST_SPAN 8U
#endif

#ifndef FLOWCAUSAL_DEFAULT_PY_TEST_BURST_WIDTH
#define FLOWCAUSAL_DEFAULT_PY_TEST_BURST_WIDTH 8U
#endif

#ifndef FLOWCAUSAL_DEFAULT_PACKAGE_AVOID_PY_TEST
#define FLOWCAUSAL_DEFAULT_PACKAGE_AVOID_PY_TEST false
#endif

#ifndef FLOWCAUSAL_DEFAULT_PACKAGE_AVOID_PY_TEST_NS
#define FLOWCAUSAL_DEFAULT_PACKAGE_AVOID_PY_TEST_NS 100000000ULL
#endif

#ifndef FLOWCAUSAL_DEFAULT_PACKAGE_AVOID_PY_TEST_ANCHOR_CPU
#define FLOWCAUSAL_DEFAULT_PACKAGE_AVOID_PY_TEST_ANCHOR_CPU 0
#endif

#ifndef FLOWCAUSAL_DEFAULT_PACKAGE_AVOID_PY_TEST_SPAN
#define FLOWCAUSAL_DEFAULT_PACKAGE_AVOID_PY_TEST_SPAN 3U
#endif

#ifndef FLOWCAUSAL_DEFAULT_FRONTIER_LOCAL_DSQ
#define FLOWCAUSAL_DEFAULT_FRONTIER_LOCAL_DSQ false
#endif

#ifndef FLOWCAUSAL_DEFAULT_CRITICAL_FRONTIER_PREEMPT
#define FLOWCAUSAL_DEFAULT_CRITICAL_FRONTIER_PREEMPT false
#endif

#ifndef FLOWCAUSAL_DEFAULT_CRITICAL_FRONTIER_BLOCKED_ONLY
#define FLOWCAUSAL_DEFAULT_CRITICAL_FRONTIER_BLOCKED_ONLY false
#endif

#ifndef FLOWCAUSAL_DEFAULT_CRITICAL_FRONTIER_COOLDOWN_NS
#define FLOWCAUSAL_DEFAULT_CRITICAL_FRONTIER_COOLDOWN_NS 0ULL
#endif

#ifndef FLOWCAUSAL_DEFAULT_CRITICAL_WAIT_FRONTIER_BYPASS
#define FLOWCAUSAL_DEFAULT_CRITICAL_WAIT_FRONTIER_BYPASS false
#endif

#ifndef FLOWCAUSAL_DEFAULT_CRITICAL_WAIT_FRONTIER_BYPASS_NS
#define FLOWCAUSAL_DEFAULT_CRITICAL_WAIT_FRONTIER_BYPASS_NS 2000000ULL
#endif

#ifndef FLOWCAUSAL_DEFAULT_CRITICAL_WAIT_FRONTIER_BYPASS_SLICE_NS
#define FLOWCAUSAL_DEFAULT_CRITICAL_WAIT_FRONTIER_BYPASS_SLICE_NS 0ULL
#endif

#ifndef FLOWCAUSAL_DEFAULT_CRITICAL_WAIT_FRONTIER_BYPASS_REQUIRE_WIDTH_HELD
#define FLOWCAUSAL_DEFAULT_CRITICAL_WAIT_FRONTIER_BYPASS_REQUIRE_WIDTH_HELD true
#endif

#ifndef FLOWCAUSAL_DEFAULT_CRITICAL_WAIT_HEAD
#define FLOWCAUSAL_DEFAULT_CRITICAL_WAIT_HEAD false
#endif

#ifndef FLOWCAUSAL_DEFAULT_CRITICAL_WAIT_HEAD_NS
#define FLOWCAUSAL_DEFAULT_CRITICAL_WAIT_HEAD_NS 2000000ULL
#endif

#ifndef FLOWCAUSAL_DEFAULT_CRITICAL_WAIT_HEAD_CXX_ONLY
#define FLOWCAUSAL_DEFAULT_CRITICAL_WAIT_HEAD_CXX_ONLY false
#endif

#ifndef FLOWCAUSAL_DEFAULT_ANCHOR_SPAN_IDLE_PICK
#define FLOWCAUSAL_DEFAULT_ANCHOR_SPAN_IDLE_PICK false
#endif

#ifndef FLOWCAUSAL_DEFAULT_COMPILE_CHAIN_BOOST
#define FLOWCAUSAL_DEFAULT_COMPILE_CHAIN_BOOST false
#endif

#ifndef FLOWCAUSAL_DEFAULT_COMPILE_CHAIN_BOOST_NS
#define FLOWCAUSAL_DEFAULT_COMPILE_CHAIN_BOOST_NS 1500000ULL
#endif

#ifndef FLOWCAUSAL_DEFAULT_COMPILE_CHAIN_LOCAL
#define FLOWCAUSAL_DEFAULT_COMPILE_CHAIN_LOCAL false
#endif

#ifndef FLOWCAUSAL_DEFAULT_COMPILE_CHAIN_SLICE
#define FLOWCAUSAL_DEFAULT_COMPILE_CHAIN_SLICE false
#endif

#ifndef FLOWCAUSAL_DEFAULT_COMPILE_CHAIN_SLICE_NS
#define FLOWCAUSAL_DEFAULT_COMPILE_CHAIN_SLICE_NS 50000000ULL
#endif

#ifndef FLOWCAUSAL_DEFAULT_CXX_FIRST_WINDOW_NS
#define FLOWCAUSAL_DEFAULT_CXX_FIRST_WINDOW_NS 0ULL
#endif

#ifndef FLOWCAUSAL_DEFAULT_CXX_BUILD_SPAN
#define FLOWCAUSAL_DEFAULT_CXX_BUILD_SPAN 3U
#endif

#ifndef FLOWCAUSAL_DEFAULT_CXX_BUILD_ANCHOR_CPU
#define FLOWCAUSAL_DEFAULT_CXX_BUILD_ANCHOR_CPU -1
#endif

#ifndef FLOWCAUSAL_DEFAULT_CXX_FIRST_SUPPRESS_CPU
#define FLOWCAUSAL_DEFAULT_CXX_FIRST_SUPPRESS_CPU 0
#endif

#ifndef FLOWCAUSAL_DEFAULT_CPUSET_LANE_MAP
#define FLOWCAUSAL_DEFAULT_CPUSET_LANE_MAP false
#endif

#ifndef FLOWCAUSAL_DEFAULT_CPUSET_LANE_SCAN_CPUS
#define FLOWCAUSAL_DEFAULT_CPUSET_LANE_SCAN_CPUS 128U
#endif

#ifndef FLOWCAUSAL_DEFAULT_CPUSET_LANE_ZERO_PREFIX_FASTPATH
#define FLOWCAUSAL_DEFAULT_CPUSET_LANE_ZERO_PREFIX_FASTPATH false
#endif

#ifndef FLOWCAUSAL_DEFAULT_CXX_FIRST_SUPPRESS_PACKAGE_ONLY
#define FLOWCAUSAL_DEFAULT_CXX_FIRST_SUPPRESS_PACKAGE_ONLY false
#endif

#ifndef FLOWCAUSAL_DEFAULT_CXX_FIRST_SUPPRESS_ROOT_COPY_ONLY
#define FLOWCAUSAL_DEFAULT_CXX_FIRST_SUPPRESS_ROOT_COPY_ONLY false
#endif

#ifndef FLOWCAUSAL_DEFAULT_CXX_FIRST_SUPPRESS_NONROOT_ONLY
#define FLOWCAUSAL_DEFAULT_CXX_FIRST_SUPPRESS_NONROOT_ONLY false
#endif

#ifndef FLOWCAUSAL_DEFAULT_FIRST_SUPPRESS_NONPACKAGE_ROOT_COPY
#define FLOWCAUSAL_DEFAULT_FIRST_SUPPRESS_NONPACKAGE_ROOT_COPY false
#endif

#ifndef FLOWCAUSAL_DEFAULT_CXX_FIRST_SUPPRESS_NONCXX_ROOT_COPY
#define FLOWCAUSAL_DEFAULT_CXX_FIRST_SUPPRESS_NONCXX_ROOT_COPY false
#endif

#ifndef FLOWCAUSAL_DEFAULT_CXX_FIRST_SUPPRESS_TO_BACKGROUND
#define FLOWCAUSAL_DEFAULT_CXX_FIRST_SUPPRESS_TO_BACKGROUND false
#endif

#ifndef FLOWCAUSAL_DEFAULT_CXX_FIRST_SUPPRESS_UNTIL_CXX_STAGE_STARTED
#define FLOWCAUSAL_DEFAULT_CXX_FIRST_SUPPRESS_UNTIL_CXX_STAGE_STARTED false
#endif

#ifndef FLOWCAUSAL_DEFAULT_ROOT_DIRECT_SEQ_STAGE
#define FLOWCAUSAL_DEFAULT_ROOT_DIRECT_SEQ_STAGE false
#endif

#ifndef FLOWCAUSAL_DEFAULT_PACKAGE_DIRECT_SEQ_STAGE
#define FLOWCAUSAL_DEFAULT_PACKAGE_DIRECT_SEQ_STAGE false
#endif

#ifndef FLOWCAUSAL_DEFAULT_PACKAGE_TEST_SEQ_STAGE_ONLY
#define FLOWCAUSAL_DEFAULT_PACKAGE_TEST_SEQ_STAGE_ONLY false
#endif

#ifndef FLOWCAUSAL_DEFAULT_CXX_UNBUDGET
#define FLOWCAUSAL_DEFAULT_CXX_UNBUDGET false
#endif

#ifndef FLOWCAUSAL_DEFAULT_PACKAGE_UNBUDGET
#define FLOWCAUSAL_DEFAULT_PACKAGE_UNBUDGET false
#endif

#ifndef FLOWCAUSAL_DEFAULT_ROOT_PREP_PARTITION
#define FLOWCAUSAL_DEFAULT_ROOT_PREP_PARTITION false
#endif

#ifndef FLOWCAUSAL_DEFAULT_CXX_BOOST_BUDGET_NS
#define FLOWCAUSAL_DEFAULT_CXX_BOOST_BUDGET_NS 0ULL
#endif

#ifndef FLOWCAUSAL_DEFAULT_PACKAGE_ROOT_COPY_BOOST_BUDGET_NS
#define FLOWCAUSAL_DEFAULT_PACKAGE_ROOT_COPY_BOOST_BUDGET_NS 0ULL
#endif

#ifndef FLOWCAUSAL_DEFAULT_PARENT_EXIT_BOOST
#define FLOWCAUSAL_DEFAULT_PARENT_EXIT_BOOST false
#endif

#ifndef FLOWCAUSAL_DEFAULT_PARENT_EXIT_BOOST_NS
#define FLOWCAUSAL_DEFAULT_PARENT_EXIT_BOOST_NS 1000000000ULL
#endif

#ifndef FLOWCAUSAL_DEFAULT_PARENT_EXIT_LATENCY
#define FLOWCAUSAL_DEFAULT_PARENT_EXIT_LATENCY false
#endif

#ifndef FLOWCAUSAL_DEFAULT_PARENT_EXIT_LATENCY_SLICE_NS
#define FLOWCAUSAL_DEFAULT_PARENT_EXIT_LATENCY_SLICE_NS 1000000ULL
#endif

#ifndef FLOWCAUSAL_DEFAULT_PARENT_EXIT_LATENCY_PREEMPT
#define FLOWCAUSAL_DEFAULT_PARENT_EXIT_LATENCY_PREEMPT true
#endif

#ifndef FLOWCAUSAL_DEFAULT_PARENT_EXIT_LATENCY_RESCUE_ONLY
#define FLOWCAUSAL_DEFAULT_PARENT_EXIT_LATENCY_RESCUE_ONLY false
#endif

#ifndef FLOWCAUSAL_DEFAULT_PARENT_EXIT_CLEANUP_DSQ
#define FLOWCAUSAL_DEFAULT_PARENT_EXIT_CLEANUP_DSQ false
#endif

#ifndef FLOWCAUSAL_DEFAULT_PARENT_EXIT_CLEANUP
#define FLOWCAUSAL_DEFAULT_PARENT_EXIT_CLEANUP false
#endif

#ifndef FLOWCAUSAL_DEFAULT_PARENT_EXIT_CLEANUP_NS
#define FLOWCAUSAL_DEFAULT_PARENT_EXIT_CLEANUP_NS 1000000000ULL
#endif

#ifndef FLOWCAUSAL_DEFAULT_PARENT_EXIT_CLEANUP_CPU
#define FLOWCAUSAL_DEFAULT_PARENT_EXIT_CLEANUP_CPU -1
#endif

#ifndef FLOWCAUSAL_DEFAULT_PARENT_EXIT_CLEANUP_CHILD_CPU
#define FLOWCAUSAL_DEFAULT_PARENT_EXIT_CLEANUP_CHILD_CPU false
#endif

#ifndef FLOWCAUSAL_DEFAULT_PARENT_EXIT_CLEANUP_SKIP_PACKAGE
#define FLOWCAUSAL_DEFAULT_PARENT_EXIT_CLEANUP_SKIP_PACKAGE false
#endif

#ifndef FLOWCAUSAL_DEFAULT_PARENT_EXIT_CLEANUP_SKIP_PY
#define FLOWCAUSAL_DEFAULT_PARENT_EXIT_CLEANUP_SKIP_PY false
#endif

#ifndef FLOWCAUSAL_DEFAULT_PARENT_EXIT_CLEANUP_SKIP_WHILE_PY_TEST_ACTIVE
#define FLOWCAUSAL_DEFAULT_PARENT_EXIT_CLEANUP_SKIP_WHILE_PY_TEST_ACTIVE false
#endif

#ifndef FLOWCAUSAL_DEFAULT_PARENT_EXIT_CLEANUP_SKIP_WHILE_PY_CRITICAL_ACTIVE
#define FLOWCAUSAL_DEFAULT_PARENT_EXIT_CLEANUP_SKIP_WHILE_PY_CRITICAL_ACTIVE false
#endif

#ifndef FLOWCAUSAL_DEFAULT_PARENT_EXIT_CLEANUP_PY_TEST_ACTIVE_NS
#define FLOWCAUSAL_DEFAULT_PARENT_EXIT_CLEANUP_PY_TEST_ACTIVE_NS 100000000ULL
#endif

#ifndef FLOWCAUSAL_DEFAULT_PARENT_EXIT_CLEANUP_CXX_ONLY
#define FLOWCAUSAL_DEFAULT_PARENT_EXIT_CLEANUP_CXX_ONLY false
#endif

#ifndef FLOWCAUSAL_DEFAULT_PARENT_EXIT_CLEANUP_CXX_ONLY_STRICT
#define FLOWCAUSAL_DEFAULT_PARENT_EXIT_CLEANUP_CXX_ONLY_STRICT false
#endif

#ifndef FLOWCAUSAL_DEFAULT_PARENT_EXIT_CLEANUP_SELECTED_ONLY
#define FLOWCAUSAL_DEFAULT_PARENT_EXIT_CLEANUP_SELECTED_ONLY false
#endif

#ifndef FLOWCAUSAL_DEFAULT_PARENT_EXIT_CLEANUP_ONESHOT
#define FLOWCAUSAL_DEFAULT_PARENT_EXIT_CLEANUP_ONESHOT false
#endif

#ifndef FLOWCAUSAL_DEFAULT_PARENT_EXIT_CLEANUP_PULSE_LIMIT
#define FLOWCAUSAL_DEFAULT_PARENT_EXIT_CLEANUP_PULSE_LIMIT 0U
#endif

#ifndef FLOWCAUSAL_DEFAULT_PARENT_EXIT_CLEANUP_ROOT_DIRECT_ONLY
#define FLOWCAUSAL_DEFAULT_PARENT_EXIT_CLEANUP_ROOT_DIRECT_ONLY false
#endif

#ifndef FLOWCAUSAL_DEFAULT_PARENT_EXIT_CLEANUP_ROOT_DIRECT_PACKAGE_ALL
#define FLOWCAUSAL_DEFAULT_PARENT_EXIT_CLEANUP_ROOT_DIRECT_PACKAGE_ALL false
#endif

#ifndef FLOWCAUSAL_DEFAULT_PARENT_EXIT_CLEANUP_PACKAGE_INTERNAL_PULSE
#define FLOWCAUSAL_DEFAULT_PARENT_EXIT_CLEANUP_PACKAGE_INTERNAL_PULSE false
#endif

#ifndef FLOWCAUSAL_DEFAULT_PARENT_EXIT_CLEANUP_PACKAGE_INTERNAL_PULSE_LIMIT
#define FLOWCAUSAL_DEFAULT_PARENT_EXIT_CLEANUP_PACKAGE_INTERNAL_PULSE_LIMIT 1U
#endif

#ifndef FLOWCAUSAL_DEFAULT_PARENT_EXIT_CLEANUP_PACKAGE_AFTER_CRITICAL
#define FLOWCAUSAL_DEFAULT_PARENT_EXIT_CLEANUP_PACKAGE_AFTER_CRITICAL false
#endif

#ifndef FLOWCAUSAL_DEFAULT_PARENT_EXIT_CLEANUP_PACKAGE_AFTER_CRITICAL_NS
#define FLOWCAUSAL_DEFAULT_PARENT_EXIT_CLEANUP_PACKAGE_AFTER_CRITICAL_NS 0ULL
#endif

#ifndef FLOWCAUSAL_DEFAULT_PARENT_EXIT_CLEANUP_PACKAGE_AFTER_CRITICAL_ONESHOT
#define FLOWCAUSAL_DEFAULT_PARENT_EXIT_CLEANUP_PACKAGE_AFTER_CRITICAL_ONESHOT false
#endif

#ifndef FLOWCAUSAL_DEFAULT_PACKAGE_BG_SEQ_MARK
#define FLOWCAUSAL_DEFAULT_PACKAGE_BG_SEQ_MARK false
#endif

#ifndef FLOWCAUSAL_DEFAULT_PARENT_EXIT_CLEANUP_PACKAGE_BG_MARK_ONLY
#define FLOWCAUSAL_DEFAULT_PARENT_EXIT_CLEANUP_PACKAGE_BG_MARK_ONLY false
#endif

#ifndef FLOWCAUSAL_DEFAULT_PACKAGE_RESCUE_AFTER_CXX_ROOT
#define FLOWCAUSAL_DEFAULT_PACKAGE_RESCUE_AFTER_CXX_ROOT false
#endif

#ifndef FLOWCAUSAL_DEFAULT_PACKAGE_RESCUE_AFTER_CXX_BUILD_CHILD
#define FLOWCAUSAL_DEFAULT_PACKAGE_RESCUE_AFTER_CXX_BUILD_CHILD false
#endif

#ifndef FLOWCAUSAL_DEFAULT_PACKAGE_RESCUE_AFTER_CXX_THIRD_CHILD
#define FLOWCAUSAL_DEFAULT_PACKAGE_RESCUE_AFTER_CXX_THIRD_CHILD false
#endif

#ifndef FLOWCAUSAL_DEFAULT_PACKAGE_RESCUE_NS
#define FLOWCAUSAL_DEFAULT_PACKAGE_RESCUE_NS 1000000000ULL
#endif

#ifndef FLOWCAUSAL_DEFAULT_PACKAGE_RESCUE_CXX_CPUSET
#define FLOWCAUSAL_DEFAULT_PACKAGE_RESCUE_CXX_CPUSET false
#endif

#ifndef FLOWCAUSAL_DEFAULT_PACKAGE_RESCUE_ANCHOR_CPU
#define FLOWCAUSAL_DEFAULT_PACKAGE_RESCUE_ANCHOR_CPU -1
#endif

#ifndef FLOWCAUSAL_DEFAULT_PACKAGE_RESCUE_SPAN
#define FLOWCAUSAL_DEFAULT_PACKAGE_RESCUE_SPAN 0U
#endif

#ifndef FLOWCAUSAL_DEFAULT_PACKAGE_BASE_ANCHOR_CPU
#define FLOWCAUSAL_DEFAULT_PACKAGE_BASE_ANCHOR_CPU -1
#endif

#ifndef FLOWCAUSAL_DEFAULT_PACKAGE_BASE_SPAN
#define FLOWCAUSAL_DEFAULT_PACKAGE_BASE_SPAN 0U
#endif

#ifndef FLOWCAUSAL_DEFAULT_PACKAGE_ROOT_COPY_ANCHOR_CPU
#define FLOWCAUSAL_DEFAULT_PACKAGE_ROOT_COPY_ANCHOR_CPU -1
#endif

#ifndef FLOWCAUSAL_DEFAULT_PACKAGE_ROOT_COPY_SPAN
#define FLOWCAUSAL_DEFAULT_PACKAGE_ROOT_COPY_SPAN 0U
#endif

#ifndef FLOWCAUSAL_DEFAULT_PY_TEST_ANCHOR_CPU
#define FLOWCAUSAL_DEFAULT_PY_TEST_ANCHOR_CPU -1
#endif

#ifndef FLOWCAUSAL_DEFAULT_PY_TEST_SPAN
#define FLOWCAUSAL_DEFAULT_PY_TEST_SPAN 0U
#endif

#ifndef FLOWCAUSAL_DEFAULT_PACKAGE_RESCUE_BACKGROUND
#define FLOWCAUSAL_DEFAULT_PACKAGE_RESCUE_BACKGROUND false
#endif

#ifndef FLOWCAUSAL_DEFAULT_PACKAGE_RESCUE_ALL_LOCALITY
#define FLOWCAUSAL_DEFAULT_PACKAGE_RESCUE_ALL_LOCALITY false
#endif

#ifndef FLOWCAUSAL_DEFAULT_PACKAGE_RESCUE_FRONTIER_GLOBAL
#define FLOWCAUSAL_DEFAULT_PACKAGE_RESCUE_FRONTIER_GLOBAL false
#endif

#ifndef FLOWCAUSAL_DEFAULT_PACKAGE_RESCUE_FALLBACK_PERCPU
#define FLOWCAUSAL_DEFAULT_PACKAGE_RESCUE_FALLBACK_PERCPU false
#endif

#ifndef FLOWCAUSAL_DEFAULT_PACKAGE_RESCUE_FALLBACK_SKIP_ROOT
#define FLOWCAUSAL_DEFAULT_PACKAGE_RESCUE_FALLBACK_SKIP_ROOT false
#endif

#ifndef FLOWCAUSAL_DEFAULT_PACKAGE_RESCUE_FALLBACK_ROOT_ONLY
#define FLOWCAUSAL_DEFAULT_PACKAGE_RESCUE_FALLBACK_ROOT_ONLY false
#endif

#ifndef FLOWCAUSAL_DEFAULT_PACKAGE_RESCUE_FALLBACK_ROOT_COPY_ONLY
#define FLOWCAUSAL_DEFAULT_PACKAGE_RESCUE_FALLBACK_ROOT_COPY_ONLY false
#endif

#ifndef FLOWCAUSAL_DEFAULT_PACKAGE_RESCUE_SHARED_PROBE
#define FLOWCAUSAL_DEFAULT_PACKAGE_RESCUE_SHARED_PROBE false
#endif

#ifndef FLOWCAUSAL_DEFAULT_PACKAGE_RESCUE_SHARED_PROBE_MIN_STREAK
#define FLOWCAUSAL_DEFAULT_PACKAGE_RESCUE_SHARED_PROBE_MIN_STREAK 0U
#endif

#ifndef FLOWCAUSAL_DEFAULT_PACKAGE_RESCUE_UNBUDGET
#define FLOWCAUSAL_DEFAULT_PACKAGE_RESCUE_UNBUDGET true
#endif

#ifndef FLOWCAUSAL_DEFAULT_PACKAGE_RESCUE_WAIT_PY_TEST
#define FLOWCAUSAL_DEFAULT_PACKAGE_RESCUE_WAIT_PY_TEST false
#endif

#ifndef FLOWCAUSAL_DEFAULT_PY_TEST_PROTECT_MAX_NS
#define FLOWCAUSAL_DEFAULT_PY_TEST_PROTECT_MAX_NS 2000000000ULL
#endif

#ifndef FLOWCAUSAL_DEFAULT_PY_TEST_PROTECT_GRACE_NS
#define FLOWCAUSAL_DEFAULT_PY_TEST_PROTECT_GRACE_NS 0ULL
#endif

#ifndef FLOWCAUSAL_DEFAULT_PACKAGE_TAIL_LOCALITY_AFTER_PY_ROOT
#define FLOWCAUSAL_DEFAULT_PACKAGE_TAIL_LOCALITY_AFTER_PY_ROOT false
#endif

#ifndef FLOWCAUSAL_DEFAULT_PACKAGE_TAIL_LOCALITY_AFTER_PY_TEST
#define FLOWCAUSAL_DEFAULT_PACKAGE_TAIL_LOCALITY_AFTER_PY_TEST false
#endif

#ifndef FLOWCAUSAL_DEFAULT_PACKAGE_TAIL_LOCALITY_NS
#define FLOWCAUSAL_DEFAULT_PACKAGE_TAIL_LOCALITY_NS 1000000000ULL
#endif

#ifndef FLOWCAUSAL_DEFAULT_PACKAGE_FINAL_TAIL_AFTER_CXX_PY_ROOT
#define FLOWCAUSAL_DEFAULT_PACKAGE_FINAL_TAIL_AFTER_CXX_PY_ROOT false
#endif

#ifndef FLOWCAUSAL_DEFAULT_PACKAGE_FINAL_TAIL_AFTER_CRITICAL_CHILDREN
#define FLOWCAUSAL_DEFAULT_PACKAGE_FINAL_TAIL_AFTER_CRITICAL_CHILDREN false
#endif

#ifndef FLOWCAUSAL_DEFAULT_PACKAGE_FINAL_TAIL_NS
#define FLOWCAUSAL_DEFAULT_PACKAGE_FINAL_TAIL_NS 1000000000ULL
#endif

#ifndef FLOWCAUSAL_DEFAULT_PACKAGE_FINAL_TAIL_ANCHOR_CPU
#define FLOWCAUSAL_DEFAULT_PACKAGE_FINAL_TAIL_ANCHOR_CPU 0
#endif

#ifndef FLOWCAUSAL_DEFAULT_PACKAGE_FINAL_TAIL_SPAN
#define FLOWCAUSAL_DEFAULT_PACKAGE_FINAL_TAIL_SPAN 8U
#endif

#ifndef FLOWCAUSAL_DEFAULT_PACKAGE_FINAL_TAIL_LOCALITY
#define FLOWCAUSAL_DEFAULT_PACKAGE_FINAL_TAIL_LOCALITY true
#endif

#ifndef FLOWCAUSAL_DEFAULT_PACKAGE_FINAL_TAIL_SHARED_SLICE_NS
#define FLOWCAUSAL_DEFAULT_PACKAGE_FINAL_TAIL_SHARED_SLICE_NS 0ULL
#endif

#ifndef FLOWCAUSAL_DEFAULT_PACKAGE_FINAL_TAIL_FALLBACK_PERCPU
#define FLOWCAUSAL_DEFAULT_PACKAGE_FINAL_TAIL_FALLBACK_PERCPU false
#endif

#ifndef FLOWCAUSAL_DEFAULT_PACKAGE_START_RESCUE_NS
#define FLOWCAUSAL_DEFAULT_PACKAGE_START_RESCUE_NS 0ULL
#endif

#ifndef FLOWCAUSAL_DEFAULT_PACKAGE_BG_PARENT_CLEANUP
#define FLOWCAUSAL_DEFAULT_PACKAGE_BG_PARENT_CLEANUP false
#endif

#ifndef FLOWCAUSAL_DEFAULT_PACKAGE_BG_PARENT_CLEANUP_CPU
#define FLOWCAUSAL_DEFAULT_PACKAGE_BG_PARENT_CLEANUP_CPU -1
#endif

#ifndef FLOWCAUSAL_DEFAULT_PACKAGE_BG_PARENT_CLEANUP_SLICE_NS
#define FLOWCAUSAL_DEFAULT_PACKAGE_BG_PARENT_CLEANUP_SLICE_NS 0ULL
#endif

#ifndef FLOWCAUSAL_DEFAULT_PACKAGE_BG_RESCUE_SHARED_SLICE_NS
#define FLOWCAUSAL_DEFAULT_PACKAGE_BG_RESCUE_SHARED_SLICE_NS 0ULL
#endif

#ifndef FLOWCAUSAL_DEFAULT_PACKAGE_ROOT_PARENT_CLEANUP
#define FLOWCAUSAL_DEFAULT_PACKAGE_ROOT_PARENT_CLEANUP false
#endif

#ifndef FLOWCAUSAL_DEFAULT_ROOT_COPY_SHARED
#define FLOWCAUSAL_DEFAULT_ROOT_COPY_SHARED false
#endif

#ifndef FLOWCAUSAL_DEFAULT_PACKAGE_ROOT_COPY_SHARED
#define FLOWCAUSAL_DEFAULT_PACKAGE_ROOT_COPY_SHARED false
#endif

#ifndef FLOWCAUSAL_DEFAULT_ROOT_COPY_LOCAL_DSQ
#define FLOWCAUSAL_DEFAULT_ROOT_COPY_LOCAL_DSQ false
#endif

#ifndef FLOWCAUSAL_DEFAULT_PACKAGE_ROOT_COPY_LOCAL_DSQ
#define FLOWCAUSAL_DEFAULT_PACKAGE_ROOT_COPY_LOCAL_DSQ false
#endif

#ifndef FLOWCAUSAL_DEFAULT_CXX_ROOT_COPY_LOCAL_DSQ
#define FLOWCAUSAL_DEFAULT_CXX_ROOT_COPY_LOCAL_DSQ false
#endif

#ifndef FLOWCAUSAL_DEFAULT_ROOT_COPY_UNBUDGET
#define FLOWCAUSAL_DEFAULT_ROOT_COPY_UNBUDGET false
#endif

#ifndef FLOWCAUSAL_DEFAULT_ROOT_COPY_SHARED_SLICE_NS
#define FLOWCAUSAL_DEFAULT_ROOT_COPY_SHARED_SLICE_NS 0ULL
#endif

#ifndef FLOWCAUSAL_DEFAULT_ROOT_COPY_DSQ
#define FLOWCAUSAL_DEFAULT_ROOT_COPY_DSQ false
#endif

#ifndef FLOWCAUSAL_DEFAULT_ROOT_COPY_DSQ_DISPATCH_QUOTA
#define FLOWCAUSAL_DEFAULT_ROOT_COPY_DSQ_DISPATCH_QUOTA 0U
#endif

#ifndef FLOWCAUSAL_DEFAULT_ROOT_COPY_SINGLE_OWNER
#define FLOWCAUSAL_DEFAULT_ROOT_COPY_SINGLE_OWNER false
#endif

#ifndef FLOWCAUSAL_DEFAULT_ROOT_COPY_MAX_OWNERS
#define FLOWCAUSAL_DEFAULT_ROOT_COPY_MAX_OWNERS 0U
#endif

#ifndef FLOWCAUSAL_DEFAULT_PACKAGE_ROOT_COPY_UNBUDGET
#define FLOWCAUSAL_DEFAULT_PACKAGE_ROOT_COPY_UNBUDGET false
#endif

#ifndef FLOWCAUSAL_DEFAULT_PACKAGE_ROOT_SHARED_SLICE_NS
#define FLOWCAUSAL_DEFAULT_PACKAGE_ROOT_SHARED_SLICE_NS 0ULL
#endif

#ifndef FLOWCAUSAL_DEFAULT_PACKAGE_ROOT_NO_FRONTIER
#define FLOWCAUSAL_DEFAULT_PACKAGE_ROOT_NO_FRONTIER false
#endif

#ifndef FLOWCAUSAL_DEFAULT_PACKAGE_ROOT_COPY_WAKEUP_PREEMPT
#define FLOWCAUSAL_DEFAULT_PACKAGE_ROOT_COPY_WAKEUP_PREEMPT false
#endif

#ifndef FLOWCAUSAL_DEFAULT_ROOT_COPY_WAKEUP_PREEMPT
#define FLOWCAUSAL_DEFAULT_ROOT_COPY_WAKEUP_PREEMPT false
#endif

#ifndef FLOWCAUSAL_DEFAULT_ROOT_COPY_WAKEUP_MIN_BLOCK_NS
#define FLOWCAUSAL_DEFAULT_ROOT_COPY_WAKEUP_MIN_BLOCK_NS 1000000ULL
#endif

#ifndef FLOWCAUSAL_DEFAULT_PACKAGE_ROOT_COPY_WAKEUP_MIN_BLOCK_NS
#define FLOWCAUSAL_DEFAULT_PACKAGE_ROOT_COPY_WAKEUP_MIN_BLOCK_NS 1000000ULL
#endif

#ifndef FLOWCAUSAL_DEFAULT_PACKAGE_ROOT_COPY_LATENCY
#define FLOWCAUSAL_DEFAULT_PACKAGE_ROOT_COPY_LATENCY false
#endif

#ifndef FLOWCAUSAL_DEFAULT_PACKAGE_ROOT_COPY_LATENCY_SLICE_NS
#define FLOWCAUSAL_DEFAULT_PACKAGE_ROOT_COPY_LATENCY_SLICE_NS 1000000ULL
#endif

#ifndef FLOWCAUSAL_DEFAULT_ROOT_CHILD_WAKEUP
#define FLOWCAUSAL_DEFAULT_ROOT_CHILD_WAKEUP false
#endif

#ifndef FLOWCAUSAL_DEFAULT_ROOT_CHILD_WAKEUP_NS
#define FLOWCAUSAL_DEFAULT_ROOT_CHILD_WAKEUP_NS 50000000ULL
#endif

#ifndef FLOWCAUSAL_DEFAULT_ROOT_CHILD_WAKEUP_PREEMPT
#define FLOWCAUSAL_DEFAULT_ROOT_CHILD_WAKEUP_PREEMPT true
#endif

#ifndef FLOWCAUSAL_DEFAULT_ROOT_CHILD_WAKEUP_ANCHOR_CPU
#define FLOWCAUSAL_DEFAULT_ROOT_CHILD_WAKEUP_ANCHOR_CPU -1
#endif

#ifndef FLOWCAUSAL_DEFAULT_ROOT_CHILD_WAKEUP_ZERO_ONLY
#define FLOWCAUSAL_DEFAULT_ROOT_CHILD_WAKEUP_ZERO_ONLY false
#endif

#ifndef FLOWCAUSAL_DEFAULT_PY_TEST_RUNTIME_TAIL_LATENCY
#define FLOWCAUSAL_DEFAULT_PY_TEST_RUNTIME_TAIL_LATENCY false
#endif

#ifndef FLOWCAUSAL_DEFAULT_PY_TEST_RUNTIME_TAIL_NS
#define FLOWCAUSAL_DEFAULT_PY_TEST_RUNTIME_TAIL_NS 120000000ULL
#endif

#ifndef FLOWCAUSAL_DEFAULT_PY_TEST_RUNTIME_TAIL_SLICE_NS
#define FLOWCAUSAL_DEFAULT_PY_TEST_RUNTIME_TAIL_SLICE_NS 0ULL
#endif

#ifndef FLOWCAUSAL_DEFAULT_PY_TEST_RUNTIME_TAIL_PREEMPT
#define FLOWCAUSAL_DEFAULT_PY_TEST_RUNTIME_TAIL_PREEMPT false
#endif

#ifndef FLOWCAUSAL_DEFAULT_PY_TEST_RUNTIME_TAIL_LOCAL
#define FLOWCAUSAL_DEFAULT_PY_TEST_RUNTIME_TAIL_LOCAL false
#endif

#ifndef FLOWCAUSAL_DEFAULT_PY_TEST_RUNTIME_TAIL_RESCUE_ONLY
#define FLOWCAUSAL_DEFAULT_PY_TEST_RUNTIME_TAIL_RESCUE_ONLY false
#endif

#ifndef FLOWCAUSAL_DEFAULT_PY_TEST_RUNTIME_TAIL_AFTER_PACKAGE_CRITICAL
#define FLOWCAUSAL_DEFAULT_PY_TEST_RUNTIME_TAIL_AFTER_PACKAGE_CRITICAL false
#endif

#ifndef FLOWCAUSAL_DEFAULT_PY_TEST_RUNTIME_TAIL_LOCAL_ANCHOR_CPU
#define FLOWCAUSAL_DEFAULT_PY_TEST_RUNTIME_TAIL_LOCAL_ANCHOR_CPU 3
#endif

#ifndef FLOWCAUSAL_DEFAULT_PY_TEST_RUNTIME_TAIL_LOCAL_SPAN
#define FLOWCAUSAL_DEFAULT_PY_TEST_RUNTIME_TAIL_LOCAL_SPAN 2U
#endif

#ifndef FLOWCAUSAL_PARENT_EXIT_SESSION_ID
#define FLOWCAUSAL_PARENT_EXIT_SESSION_ID 0x9e3779b97f4a7c15ULL
#endif

const volatile bool fifo_sched;
const volatile __u32 default_min_confidence = 70;
const volatile __u32 default_max_vtime_boost = 500000;
const volatile __u64 default_window_ns = 100000000ULL;
const volatile __u32 default_frontier_dispatch_quota = 4;
const volatile __u32 default_guard_cooldown_dispatches;
const volatile __u32 default_pressure_fallback_enqueues;
const volatile __u64 default_pressure_fallback_ns;
const volatile __u64 default_shared_wait_pressure_ns;
const volatile __u64 default_pressure_dispatch_ns;
const volatile bool default_pressure_local_cpu;
const volatile bool default_locality_frontier_only;
const volatile bool default_locality_remembered_first;
const volatile __u64 default_frontier_dispatch_window_ns;
const volatile __u32 default_frontier_dispatch_window_budget;
const volatile __u64 default_shared_slice_ns;
const volatile bool default_shared_wakeup_preempt;
const volatile __u64 default_shared_wakeup_slice_ns;
const volatile __u64 default_shared_wakeup_min_block_ns;
const volatile bool default_frontier_wakeup_preempt;
const volatile bool default_shared_local_dsq;
const volatile bool default_tracked_local_dsq;
const volatile bool default_frontier_fallback_local_dsq;
const volatile bool default_shared_percpu_dsq;
const volatile bool default_background_dsq;
const volatile __u32 default_background_dispatch_quota;
const volatile bool default_mode_aware = FLOWCAUSAL_DEFAULT_MODE_AWARE;
const volatile bool default_bpf_auto_frontier =
	FLOWCAUSAL_DEFAULT_BPF_AUTO_FRONTIER;
const volatile __u32 default_bpf_auto_confidence = 80;
const volatile __u32 default_control_event_mask =
	FLOWCAUSAL_DEFAULT_CONTROL_EVENT_MASK;
const volatile bool default_respect_c_assist =
	FLOWCAUSAL_DEFAULT_RESPECT_C_ASSIST;
const volatile bool default_converge = FLOWCAUSAL_DEFAULT_CONVERGE;
const volatile bool default_session_frontier_dsq =
	FLOWCAUSAL_DEFAULT_SESSION_FRONTIER_DSQ;
const volatile bool default_adaptive_width =
	FLOWCAUSAL_DEFAULT_ADAPTIVE_WIDTH;
const volatile bool default_deadline_vtime =
	FLOWCAUSAL_DEFAULT_DEADLINE_VTIME;
const volatile bool default_converge_tail_dsq =
	FLOWCAUSAL_DEFAULT_CONVERGE_TAIL_DSQ;
const volatile bool default_frontier_local_dsq =
	FLOWCAUSAL_DEFAULT_FRONTIER_LOCAL_DSQ;
const volatile bool default_compile_chain_boost =
	FLOWCAUSAL_DEFAULT_COMPILE_CHAIN_BOOST;
const volatile __u64 default_compile_chain_boost_ns =
	FLOWCAUSAL_DEFAULT_COMPILE_CHAIN_BOOST_NS;
const volatile bool default_compile_chain_local =
	FLOWCAUSAL_DEFAULT_COMPILE_CHAIN_LOCAL;
const volatile __u64 default_cxx_first_window_ns =
	FLOWCAUSAL_DEFAULT_CXX_FIRST_WINDOW_NS;
const volatile __u32 default_converge_tail_runnable = 2;
const volatile __u32 default_converge_tail_width = 1;
const volatile __u32 default_converge_wide_width = 4;
const volatile __u64 default_converge_wait_ns = 2000000ULL;

static __u64 vtime_now;
static __u32 frontier_dispatch_streak;
static __u32 shared_guard_cooldown_remaining;
static __u32 pressure_fallback_remaining;
static __u64 pressure_fallback_until_ns;
static __u64 pressure_dispatch_until_ns;
static __s32 pressure_cpu = -1;
static __u64 frontier_dispatch_window_start_ns;
static __u32 frontier_dispatch_window_used;
static __u32 background_dispatch_streak;
static __u32 root_copy_dispatch_streak;
static __u32 session_frontier_rr_slot;
static bool shared_wakeup_preempt_dispatch_pending;
static __u64 scheduler_start_ns;
static __u64 package_rescue_until_ns;
static __u64 package_tail_locality_until_ns;
static __u64 package_final_tail_until_ns;
static __u64 py_test_protect_until_ns;
static __u64 root_copy_owner_session;
static __u64 root_copy_owner_session2;
static bool cxx_root_exited;
static bool py_root_exited;
static bool py_critical_child_started;
static bool cxx_critical_child_exited;
static bool py_critical_child_exited;
static bool package_critical_child_exited;
static bool cxx_root_stage_started;
UEI_DEFINE(uei);

enum flow_global_stat_id {
	FLOW_GSTAT_FRONTIER_DISPATCHES = 0,
	FLOW_GSTAT_SHARED_DISPATCHES = 1,
	FLOW_GSTAT_SHARED_FIRST_DISPATCHES = 2,
	FLOW_GSTAT_FRONTIER_GUARD_TRIPS = 3,
	FLOW_GSTAT_FRONTIER_GUARD_MISSES = 4,
	FLOW_GSTAT_SHARED_COOLDOWN_ENTRIES = 5,
	FLOW_GSTAT_SHARED_COOLDOWN_DISPATCHES = 6,
	FLOW_GSTAT_SHARED_COOLDOWN_MISSES = 7,
	FLOW_GSTAT_PRESSURE_FALLBACK_ENTRIES = 8,
	FLOW_GSTAT_PRESSURE_FALLBACK_ENQUEUES = 9,
	FLOW_GSTAT_FRONTIER_BUDGET_WINDOWS = 10,
	FLOW_GSTAT_FRONTIER_BUDGET_TRIPS = 11,
	FLOW_GSTAT_FRONTIER_BUDGET_SHARED_DISPATCHES = 12,
	FLOW_GSTAT_FRONTIER_BUDGET_MISSES = 13,
	FLOW_GSTAT_SHARED_WAIT_PRESSURE_EVENTS = 14,
	FLOW_GSTAT_SHARED_WAIT_PRESSURE_WAIT_NS = 15,
	FLOW_GSTAT_PRESSURE_DISPATCH_ENTRIES = 16,
	FLOW_GSTAT_PRESSURE_DISPATCHES = 17,
	FLOW_GSTAT_PRESSURE_DISPATCH_MISSES = 18,
	FLOW_GSTAT_SHARED_WAKEUP_PREEMPT_ENTRIES = 19,
	FLOW_GSTAT_SHARED_WAKEUP_PREEMPT_DISPATCHES = 20,
	FLOW_GSTAT_SHARED_WAKEUP_PREEMPT_MISSES = 21,
	FLOW_GSTAT_SHARED_LOCAL_ENQUEUES = 22,
	FLOW_GSTAT_SHARED_PERCPU_ENQUEUES = 23,
	FLOW_GSTAT_SHARED_PERCPU_DISPATCHES = 24,
	FLOW_GSTAT_SHARED_PERCPU_MISSES = 25,
	FLOW_GSTAT_FRONTIER_WAKEUP_PREEMPT_ENTRIES = 26,
	FLOW_GSTAT_BACKGROUND_ENQUEUES = 27,
	FLOW_GSTAT_BACKGROUND_DISPATCHES = 28,
	FLOW_GSTAT_FRONTIER_DSQ_ENQUEUES = 29,
	FLOW_GSTAT_TRACKED_SHARED_PATH_ENQUEUES = 30,
	FLOW_GSTAT_UNTRACKED_SHARED_PATH_ENQUEUES = 31,
	FLOW_GSTAT_SHARED_GLOBAL_ENQUEUES = 32,
	FLOW_GSTAT_SHARED_GLOBAL_TRACKED_ENQUEUES = 33,
	FLOW_GSTAT_SHARED_GLOBAL_UNTRACKED_ENQUEUES = 34,
	FLOW_GSTAT_LATENCY_DSQ_ENQUEUES = 35,
	FLOW_GSTAT_SHARED_LOCAL_TRACKED_ENQUEUES = 36,
	FLOW_GSTAT_SHARED_LOCAL_UNTRACKED_ENQUEUES = 37,
	FLOW_GSTAT_SHARED_PERCPU_TRACKED_ENQUEUES = 38,
	FLOW_GSTAT_SHARED_PERCPU_UNTRACKED_ENQUEUES = 39,
	FLOW_GSTAT_SHARED_LOCAL_PICK_FALLBACKS = 40,
	FLOW_GSTAT_SHARED_PERCPU_PICK_FALLBACKS = 41,
	FLOW_GSTAT_SHARED_LOCAL_FRONTIER_FALLBACK_ENQUEUES = 42,
	FLOW_GSTAT_CONVERGE_DSQ_ENQUEUES = 51,
	FLOW_GSTAT_CONVERGE_DISPATCHES = 52,
	FLOW_GSTAT_SESSION_FRONTIER_DSQ_ENQUEUES = 53,
	FLOW_GSTAT_SESSION_FRONTIER_DISPATCHES = 54,
	FLOW_GSTAT_ADAPTIVE_WIDTH_TAIL_ADMISSIONS = 55,
	FLOW_GSTAT_ADAPTIVE_WIDTH_WIDE_ADMISSIONS = 56,
	FLOW_GSTAT_ADAPTIVE_WIDTH_HELD_ENQUEUES = 57,
	FLOW_GSTAT_ROOT_COPY_DSQ_ENQUEUES = 58,
	FLOW_GSTAT_ROOT_COPY_DISPATCHES = 59,
};

struct {
	__uint(type, BPF_MAP_TYPE_HASH);
	__type(key, struct flow_task_key);
	__type(value, struct flow_task_ctx);
	__uint(max_entries, 131072);
} task_ctx SEC(".maps");

struct {
	__uint(type, BPF_MAP_TYPE_HASH);
	__type(key, __u64);
	__type(value, struct flow_session_policy);
	__uint(max_entries, 4096);
} session_policy SEC(".maps");

struct {
	__uint(type, BPF_MAP_TYPE_HASH);
	__type(key, __u64);
	__type(value, struct flow_session_stats);
	__uint(max_entries, 4096);
} session_stats SEC(".maps");

struct flow_session_runtime {
	__u64 runnable_tasks;
	__u64 forked_tasks;
	__u64 exited_tasks;
	__u64 last_tail_ns;
	__u64 critical_child_done_mask;
};

#define FLOWCAUSAL_RUNTIME_PACKAGE_CRITICAL_DONE (1ULL << 0)
#define FLOWCAUSAL_RUNTIME_PACKAGE_POSTCRIT_CLEANUP_USED (1ULL << 1)

struct {
	__uint(type, BPF_MAP_TYPE_HASH);
	__type(key, __u64);
	__type(value, struct flow_session_runtime);
	__uint(max_entries, 4096);
} session_runtime SEC(".maps");

struct {
	__uint(type, BPF_MAP_TYPE_ARRAY);
	__type(key, __u32);
	__type(value, struct flow_global_stats);
	__uint(max_entries, 1);
} global_stats SEC(".maps");

struct {
	__uint(type, BPF_MAP_TYPE_HASH);
	__type(key, struct flow_task_key);
	__type(value, __u64);
	__uint(max_entries, 131072);
} width_active SEC(".maps");

struct {
	__uint(type, BPF_MAP_TYPE_HASH);
	__type(key, struct flow_task_key);
	__type(value, __u64);
	__uint(max_entries, 131072);
} shared_wait_start SEC(".maps");

struct {
	__uint(type, BPF_MAP_TYPE_HASH);
	__type(key, __u64);
	__type(value, __u64);
	__uint(max_entries, 4096);
} critical_frontier_preempt_last SEC(".maps");

struct {
	__uint(type, BPF_MAP_TYPE_HASH);
	__type(key, struct flow_task_key);
	__type(value, __u64);
	__uint(max_entries, 131072);
} root_child_wakeup_until SEC(".maps");

struct {
	__uint(type, BPF_MAP_TYPE_HASH);
	__type(key, struct flow_task_key);
	__type(value, __u32);
	__uint(max_entries, 131072);
} root_active_children SEC(".maps");

struct {
	__uint(type, BPF_MAP_TYPE_HASH);
	__type(key, struct flow_task_key);
	__type(value, __u32);
	__uint(max_entries, 131072);
} root_direct_child_seq SEC(".maps");

struct flow_sched_task_state {
	__u64 last_slice_ns;
	__u64 blocked_since_ns;
	__s32 selected_cpu;
	bool last_blocked;
	bool queued;
};

struct {
	__uint(type, BPF_MAP_TYPE_TASK_STORAGE);
	__uint(map_flags, BPF_F_NO_PREALLOC);
	__type(key, int);
	__type(value, struct flow_sched_task_state);
} sched_task_state SEC(".maps");

struct {
	__uint(type, BPF_MAP_TYPE_RINGBUF);
	__uint(max_entries, 1 << 24);
} events SEC(".maps");

static __always_inline struct flow_session_policy default_policy(void)
{
	struct flow_session_policy p = {};

	p.flags = 0;
	p.min_confidence = default_min_confidence;
	p.max_frontier_width = 1;
	p.max_vtime_boost = default_max_vtime_boost;
	p.window_ns = default_window_ns;
	p.boost_budget_ns = default_window_ns / 10;
	p.expires_ns = 0;
	p.anchor_cpu = -1;
	p.anchor_span = 1;
	return p;
}

static __always_inline struct flow_session_policy
lookup_policy_or_default(__u64 session_id)
{
	struct flow_session_policy *p;

	p = bpf_map_lookup_elem(&session_policy, &session_id);
	if (p)
		return *p;
	return default_policy();
}

static __always_inline struct flow_session_policy
lookup_policy_or_default_ctx(struct flow_task_ctx *ctx)
{
	if (!ctx)
		return default_policy();
	return lookup_policy_or_default(ctx->session_id);
}

static __always_inline __u32 clamp_confidence(__u32 confidence)
{
	if (confidence > 100)
		return 100;
	return confidence;
}

static __always_inline __u32 task_mode_bits(__u32 flags)
{
	return flags & FLOWCAUSAL_TASK_MODE_MASK;
}

static __always_inline bool comm_prefix_match(const char comm[FLOWCAUSAL_MAX_COMM],
					      const char *prefix, __u32 len)
{
	__u32 i;

#pragma unroll
	for (i = 0; i < FLOWCAUSAL_MAX_COMM; i++) {
		if (i >= len)
			return true;
		if (!comm[i] || comm[i] != prefix[i])
			return false;
	}
	return false;
}

static __always_inline bool comm_is(const char comm[FLOWCAUSAL_MAX_COMM],
				    const char *prefix, __u32 len)
{
	return comm_prefix_match(comm, prefix, len);
}

static __always_inline __u32 comm_mode_hint(const char comm[FLOWCAUSAL_MAX_COMM])
{
	if (!comm[0])
		return FLOWCAUSAL_TASK_MODE_UNKNOWN;

	if (comm_is(comm, "gcc", sizeof("gcc") - 1) ||
	    comm_is(comm, "g++", sizeof("g++") - 1) ||
	    comm_is(comm, "clang", sizeof("clang") - 1) ||
	    comm_is(comm, "clang++", sizeof("clang++") - 1) ||
	    comm_is(comm, "cc", sizeof("cc") - 1) ||
	    comm_is(comm, "cc1", sizeof("cc1") - 1) ||
	    comm_is(comm, "cc1plus", sizeof("cc1plus") - 1) ||
	    comm_is(comm, "rustc", sizeof("rustc") - 1) ||
	    comm_is(comm, "javac", sizeof("javac") - 1) ||
	    comm_is(comm, "pdflatex", sizeof("pdflatex") - 1) ||
	    comm_is(comm, "flowbench", sizeof("flowbench") - 1) ||
	    comm_is(comm, "test_runner", sizeof("test_runner") - 1) ||
	    comm_is(comm, "ld", sizeof("ld") - 1) ||
	    comm_is(comm, "ld.lld", sizeof("ld.lld") - 1) ||
	    comm_is(comm, "lld", sizeof("lld") - 1) ||
	    comm_is(comm, "mold", sizeof("mold") - 1) ||
	    comm_is(comm, "gold", sizeof("gold") - 1) ||
	    comm_is(comm, "collect2", sizeof("collect2") - 1) ||
	    comm_is(comm, "ar", sizeof("ar") - 1) ||
	    comm_is(comm, "as", sizeof("as") - 1) ||
	    comm_is(comm, "strip", sizeof("strip") - 1) ||
	    comm_is(comm, "llvm-strip", sizeof("llvm-strip") - 1) ||
	    comm_is(comm, "objcopy", sizeof("objcopy") - 1) ||
	    comm_is(comm, "objdump", sizeof("objdump") - 1) ||
	    comm_is(comm, "nm", sizeof("nm") - 1) ||
	    comm_is(comm, "ranlib", sizeof("ranlib") - 1))
		return FLOWCAUSAL_TASK_MODE_COMPUTE;

	if (comm_is(comm, "make", sizeof("make") - 1) ||
	    comm_is(comm, "ninja", sizeof("ninja") - 1) ||
	    comm_is(comm, "cmake", sizeof("cmake") - 1) ||
	    comm_is(comm, "cargo", sizeof("cargo") - 1) ||
	    comm_is(comm, "go", sizeof("go") - 1) ||
	    comm_is(comm, "mvn", sizeof("mvn") - 1) ||
	    comm_is(comm, "gradle", sizeof("gradle") - 1) ||
	    comm_is(comm, "pytest", sizeof("pytest") - 1) ||
	    comm_is(comm, "ctest", sizeof("ctest") - 1) ||
	    comm_is(comm, "jest", sizeof("jest") - 1) ||
	    comm_is(comm, "bats", sizeof("bats") - 1) ||
	    comm_is(comm, "lit", sizeof("lit") - 1) ||
	    comm_is(comm, "pip", sizeof("pip") - 1) ||
	    comm_is(comm, "docker", sizeof("docker") - 1) ||
	    comm_is(comm, "git", sizeof("git") - 1) ||
	    comm_is(comm, "git-remote-http", sizeof("git-remote-http") - 1) ||
	    comm_is(comm, "sudo", sizeof("sudo") - 1) ||
	    comm_is(comm, "bpftool", sizeof("bpftool") - 1) ||
	    comm_is(comm, "tar", sizeof("tar") - 1) ||
	    comm_is(comm, "gzip", sizeof("gzip") - 1) ||
	    comm_is(comm, "find", sizeof("find") - 1) ||
	    comm_is(comm, "grep", sizeof("grep") - 1) ||
	    comm_is(comm, "xargs", sizeof("xargs") - 1) ||
	    comm_is(comm, "hg", sizeof("hg") - 1) ||
	    comm_is(comm, "bash", sizeof("bash") - 1) ||
	    comm_is(comm, "sh", sizeof("sh") - 1) ||
	    comm_is(comm, "zsh", sizeof("zsh") - 1) ||
	    comm_is(comm, "fish", sizeof("fish") - 1) ||
	    comm_is(comm, "vim", sizeof("vim") - 1) ||
	    comm_is(comm, "vi", sizeof("vi") - 1) ||
	    comm_is(comm, "nano", sizeof("nano") - 1) ||
	    comm_is(comm, "emacs", sizeof("emacs") - 1) ||
	    comm_is(comm, "code", sizeof("code") - 1) ||
	    comm_is(comm, "codex", sizeof("codex") - 1) ||
	    comm_is(comm, "agent", sizeof("agent") - 1) ||
	    comm_is(comm, "openhands", sizeof("openhands") - 1) ||
	    comm_is(comm, "swe-agent", sizeof("swe-agent") - 1) ||
	    comm_is(comm, "python", sizeof("python") - 1) ||
	    comm_is(comm, "python3", sizeof("python3") - 1) ||
	    comm_is(comm, "ipython", sizeof("ipython") - 1) ||
	    comm_is(comm, "jupyter", sizeof("jupyter") - 1) ||
	    comm_is(comm, "node", sizeof("node") - 1) ||
	    comm_is(comm, "nodejs", sizeof("nodejs") - 1) ||
	    comm_is(comm, "npm", sizeof("npm") - 1) ||
	    comm_is(comm, "ruby", sizeof("ruby") - 1) ||
	    comm_is(comm, "perl", sizeof("perl") - 1) ||
	    comm_is(comm, "lua", sizeof("lua") - 1))
		return FLOWCAUSAL_TASK_MODE_INTERACTIVE;

	return FLOWCAUSAL_TASK_MODE_UNKNOWN;
}

static __always_inline bool comm_is_compile_chain(const char comm[FLOWCAUSAL_MAX_COMM])
{
	if (!comm[0])
		return false;

	return comm_is(comm, "gcc", sizeof("gcc") - 1) ||
	       comm_is(comm, "g++", sizeof("g++") - 1) ||
	       comm_is(comm, "clang", sizeof("clang") - 1) ||
	       comm_is(comm, "clang++", sizeof("clang++") - 1) ||
	       comm_is(comm, "cc", sizeof("cc") - 1) ||
	       comm_is(comm, "cc1", sizeof("cc1") - 1) ||
	       comm_is(comm, "cc1plus", sizeof("cc1plus") - 1) ||
	       comm_is(comm, "rustc", sizeof("rustc") - 1) ||
	       comm_is(comm, "ld", sizeof("ld") - 1) ||
	       comm_is(comm, "ld.lld", sizeof("ld.lld") - 1) ||
	       comm_is(comm, "lld", sizeof("lld") - 1) ||
	       comm_is(comm, "mold", sizeof("mold") - 1) ||
	       comm_is(comm, "gold", sizeof("gold") - 1) ||
	       comm_is(comm, "collect2", sizeof("collect2") - 1) ||
	       comm_is(comm, "ar", sizeof("ar") - 1) ||
	       comm_is(comm, "as", sizeof("as") - 1) ||
	       comm_is(comm, "ranlib", sizeof("ranlib") - 1);
}

static __always_inline void set_task_mode(struct flow_task_ctx *ctx, __u32 mode)
{
	if (!ctx || !default_mode_aware)
		return;
	ctx->flags &= ~FLOWCAUSAL_TASK_MODE_MASK;
	ctx->flags |= mode;
}

static __always_inline __u32 infer_task_mode(struct flow_task_ctx *ctx,
					     struct task_struct *p)
{
	__u32 mode = FLOWCAUSAL_TASK_MODE_UNKNOWN;
	__u32 comm_mode = FLOWCAUSAL_TASK_MODE_UNKNOWN;
	__u64 runtime;
	__u64 wait;
	char comm[FLOWCAUSAL_MAX_COMM] = {};

	if (!ctx || !default_mode_aware)
		return FLOWCAUSAL_TASK_MODE_UNKNOWN;

	if (ctx->flags & FLOWCAUSAL_TASK_STAGE_PACKAGE_BG) {
		mode = FLOWCAUSAL_TASK_MODE_BACKGROUND;
		set_task_mode(ctx, mode);
		return mode;
	}
	if (ctx->flags & FLOWCAUSAL_TASK_STAGE_TEST_TAIL) {
		mode = FLOWCAUSAL_TASK_MODE_TAIL;
		set_task_mode(ctx, mode);
		return mode;
	}
	if (ctx->flags & FLOWCAUSAL_TASK_STAGE_BUILD_WIDE) {
		mode = FLOWCAUSAL_TASK_MODE_COMPUTE;
		set_task_mode(ctx, mode);
		return mode;
	}
	if (ctx->flags & FLOWCAUSAL_TASK_STAGE_INSPECT) {
		mode = FLOWCAUSAL_TASK_MODE_INTERACTIVE;
		set_task_mode(ctx, mode);
		return mode;
	}

	mode = task_mode_bits(ctx->flags);
	if (mode)
		return mode;

	if (ctx->flags & FLOWCAUSAL_TASK_BACKGROUND)
		mode = FLOWCAUSAL_TASK_MODE_BACKGROUND;
	else if (ctx->flags & (FLOWCAUSAL_TASK_FRONTIER |
			       FLOWCAUSAL_TASK_COHORT_TAIL |
			       FLOWCAUSAL_TASK_WIDTH_HELD |
			       FLOWCAUSAL_TASK_PRESSURE_HELD))
		mode = FLOWCAUSAL_TASK_MODE_TAIL;
	else if (p && bpf_probe_read_kernel_str(comm, sizeof(comm), p->comm) > 0)
		comm_mode = comm_mode_hint(comm);

	if (!mode) {
		runtime = ctx->runtime_ns;
		wait = ctx->runnable_wait_ns;
		if (comm_mode == FLOWCAUSAL_TASK_MODE_COMPUTE) {
			mode = FLOWCAUSAL_TASK_MODE_COMPUTE;
		} else if (comm_mode == FLOWCAUSAL_TASK_MODE_INTERACTIVE &&
			   runtime < SCX_SLICE_DFL &&
			   runtime <= wait + (SCX_SLICE_DFL >> 1)) {
			mode = FLOWCAUSAL_TASK_MODE_INTERACTIVE;
		} else if (runtime >= SCX_SLICE_DFL ||
			   runtime > wait + (SCX_SLICE_DFL >> 1)) {
			mode = FLOWCAUSAL_TASK_MODE_COMPUTE;
		} else {
			mode = FLOWCAUSAL_TASK_MODE_INTERACTIVE;
		}
	}

	set_task_mode(ctx, mode);
	return mode;
}

static __always_inline __u32 auto_task_mode_hint(struct flow_task_ctx *ctx,
						 struct task_struct *p)
{
	__u32 comm_mode = FLOWCAUSAL_TASK_MODE_UNKNOWN;
	__u64 runtime;
	__u64 wait;
	char comm[FLOWCAUSAL_MAX_COMM] = {};

	if (!ctx || !p)
		return FLOWCAUSAL_TASK_MODE_UNKNOWN;

	if (p && bpf_probe_read_kernel_str(comm, sizeof(comm), p->comm) > 0)
		comm_mode = comm_mode_hint(comm);

	runtime = ctx->runtime_ns;
	wait = ctx->runnable_wait_ns;

	if (comm_mode == FLOWCAUSAL_TASK_MODE_COMPUTE)
		return FLOWCAUSAL_TASK_MODE_COMPUTE;
	if (runtime >= (SCX_SLICE_DFL << 1) ||
	    runtime > wait + (SCX_SLICE_DFL >> 1))
		return FLOWCAUSAL_TASK_MODE_COMPUTE;
	if (comm_mode == FLOWCAUSAL_TASK_MODE_INTERACTIVE)
		return FLOWCAUSAL_TASK_MODE_INTERACTIVE;
	if (wait >= SCX_SLICE_DFL >> 1)
		return FLOWCAUSAL_TASK_MODE_INTERACTIVE;
	return FLOWCAUSAL_TASK_MODE_UNKNOWN;
}

static __always_inline void clear_auto_flags(struct flow_task_ctx *ctx)
{
	if (!ctx)
		return;
	ctx->flags &= ~(FLOWCAUSAL_TASK_FRONTIER |
			FLOWCAUSAL_TASK_COHORT_TAIL |
			FLOWCAUSAL_TASK_PROPAGATE |
			FLOWCAUSAL_TASK_BACKGROUND |
			FLOWCAUSAL_TASK_OBSERVE_ONLY |
			FLOWCAUSAL_TASK_BPF_AUTO);
}

static __always_inline bool package_rescue_active_now(__u64 now);
static __always_inline struct flow_session_runtime *
lookup_or_create_session_runtime(__u64 session_id);

static __always_inline bool py_test_protect_active_now(__u64 now)
{
	if (!FLOWCAUSAL_DEFAULT_PACKAGE_RESCUE_WAIT_PY_TEST)
		return false;
	if (!py_test_protect_until_ns)
		return false;
	if (!time_before(now, py_test_protect_until_ns))
		return false;
	return true;
}

static __always_inline bool py_test_session(struct flow_task_ctx *ctx,
					    struct flow_session_policy *policy)
{
	return ctx && policy &&
	       policy->anchor_cpu == 4 &&
	       (ctx->flags & FLOWCAUSAL_TASK_STAGE_TEST_TAIL);
}

static __always_inline void mark_py_test_burst_start(struct flow_task_ctx *ctx,
						     struct flow_session_policy *policy,
						     __u64 now)
{
	struct flow_session_runtime *s;

	if (!FLOWCAUSAL_DEFAULT_PY_TEST_BURST_NS)
		return;
	if (!py_test_session(ctx, policy))
		return;

	s = lookup_or_create_session_runtime(ctx->session_id);
	if (!s)
		return;
	if (!s->last_tail_ns)
		s->last_tail_ns = now;
}

static __always_inline bool py_test_burst_active_now(struct flow_task_ctx *ctx,
						     struct flow_session_policy *policy,
						     __u64 now)
{
	struct flow_session_runtime *s;

	if (!FLOWCAUSAL_DEFAULT_PY_TEST_BURST_NS)
		return false;
	if (!py_test_session(ctx, policy))
		return false;

	s = bpf_map_lookup_elem(&session_runtime, &ctx->session_id);
	if (!s || !s->last_tail_ns)
		return false;
	return time_before(now, s->last_tail_ns +
			   FLOWCAUSAL_DEFAULT_PY_TEST_BURST_NS);
}

static __always_inline bool package_avoid_py_test_active_now(__u64 now)
{
	if (!FLOWCAUSAL_DEFAULT_PACKAGE_AVOID_PY_TEST)
		return false;
	if (!py_test_protect_until_ns)
		return false;
	return time_before(now, py_test_protect_until_ns);
}

static __always_inline bool py_test_cleanup_guard_active_now(__u64 now)
{
	if (!FLOWCAUSAL_DEFAULT_PARENT_EXIT_CLEANUP_SKIP_WHILE_PY_TEST_ACTIVE)
		return false;
	if (!py_test_protect_until_ns)
		return false;
	return time_before(now, py_test_protect_until_ns);
}

static __always_inline bool py_critical_cleanup_guard_active(void)
{
	if (!FLOWCAUSAL_DEFAULT_PARENT_EXIT_CLEANUP_SKIP_WHILE_PY_CRITICAL_ACTIVE)
		return false;
	return py_critical_child_started && !py_critical_child_exited;
}

static __always_inline void refresh_py_test_cleanup_guard(struct flow_task_ctx *ctx,
							  struct flow_session_policy *policy,
							  __u64 now)
{
	if (!FLOWCAUSAL_DEFAULT_PARENT_EXIT_CLEANUP_SKIP_WHILE_PY_TEST_ACTIVE)
		return;
	if (!py_test_session(ctx, policy))
		return;
	py_test_protect_until_ns =
		now + FLOWCAUSAL_DEFAULT_PARENT_EXIT_CLEANUP_PY_TEST_ACTIVE_NS;
}

static __always_inline void refresh_package_avoid_py_test(struct flow_task_ctx *ctx,
							  struct flow_session_policy *policy,
							  __u64 now)
{
	if (!FLOWCAUSAL_DEFAULT_PACKAGE_AVOID_PY_TEST)
		return;
	if (!py_test_session(ctx, policy))
		return;
	py_test_protect_until_ns = now + FLOWCAUSAL_DEFAULT_PACKAGE_AVOID_PY_TEST_NS;
}

static __always_inline void apply_bpf_auto_classification(struct task_struct *p,
							  struct flow_task_ctx *ctx,
							  struct flow_session_policy *policy,
							  __u64 now)
{
	__u32 mode;

	if (!default_bpf_auto_frontier || !ctx || !policy)
		return;
	if (ctx->flags & FLOWCAUSAL_TASK_STAGE_PACKAGE_BG) {
		if (FLOWCAUSAL_DEFAULT_PACKAGE_RESCUE_BACKGROUND &&
		    policy->anchor_cpu == 5 &&
		    package_rescue_active_now(now)) {
			ctx->flags &= ~(FLOWCAUSAL_TASK_COHORT_TAIL |
					FLOWCAUSAL_TASK_CONVERGE_TAIL |
					FLOWCAUSAL_TASK_BACKGROUND |
					FLOWCAUSAL_TASK_OBSERVE_ONLY |
					FLOWCAUSAL_TASK_STAGE_PACKAGE_BG);
			ctx->flags |= FLOWCAUSAL_TASK_STAGE_BUILD_WIDE |
				      FLOWCAUSAL_TASK_FRONTIER |
				      FLOWCAUSAL_TASK_PROPAGATE |
				      FLOWCAUSAL_TASK_BPF_AUTO;
			if (ctx->confidence < default_bpf_auto_confidence)
				ctx->confidence = default_bpf_auto_confidence;
			if (ctx->confidence < policy->min_confidence)
				ctx->confidence = policy->min_confidence;
			ctx->last_update_ns = now;
			return;
		}
		ctx->flags &= ~(FLOWCAUSAL_TASK_FRONTIER |
				FLOWCAUSAL_TASK_COHORT_TAIL |
				FLOWCAUSAL_TASK_PROPAGATE |
				FLOWCAUSAL_TASK_CONVERGE_TAIL);
		ctx->flags |= FLOWCAUSAL_TASK_BACKGROUND |
			      FLOWCAUSAL_TASK_BPF_AUTO;
		if (ctx->confidence < policy->min_confidence)
			ctx->confidence = policy->min_confidence;
		ctx->last_update_ns = now;
		return;
	}
	if (ctx->flags & FLOWCAUSAL_TASK_STAGE_INSPECT) {
		ctx->flags &= ~(FLOWCAUSAL_TASK_FRONTIER |
				FLOWCAUSAL_TASK_COHORT_TAIL |
				FLOWCAUSAL_TASK_PROPAGATE |
				FLOWCAUSAL_TASK_CONVERGE_TAIL |
				FLOWCAUSAL_TASK_BACKGROUND);
		ctx->flags |= FLOWCAUSAL_TASK_BPF_AUTO;
		if (ctx->confidence < policy->min_confidence)
			ctx->confidence = policy->min_confidence;
		ctx->last_update_ns = now;
		return;
	}
	if (ctx->flags & FLOWCAUSAL_TASK_STAGE_BUILD_WIDE) {
		ctx->flags |= FLOWCAUSAL_TASK_FRONTIER |
			      FLOWCAUSAL_TASK_PROPAGATE |
			      FLOWCAUSAL_TASK_BPF_AUTO;
		ctx->flags &= ~(FLOWCAUSAL_TASK_COHORT_TAIL |
				FLOWCAUSAL_TASK_CONVERGE_TAIL |
				FLOWCAUSAL_TASK_BACKGROUND);
		if (ctx->confidence < default_bpf_auto_confidence)
			ctx->confidence = default_bpf_auto_confidence;
		if (ctx->confidence < policy->min_confidence)
			ctx->confidence = policy->min_confidence;
		ctx->last_update_ns = now;
		return;
	}
	if (ctx->flags & FLOWCAUSAL_TASK_STAGE_TEST_TAIL) {
		mark_py_test_burst_start(ctx, policy, now);
		refresh_package_avoid_py_test(ctx, policy, now);
		refresh_py_test_cleanup_guard(ctx, policy, now);
		if (FLOWCAUSAL_DEFAULT_PY_TEST_NO_FRONTIER &&
		    policy->anchor_cpu == 4) {
			ctx->flags &= ~(FLOWCAUSAL_TASK_FRONTIER |
					FLOWCAUSAL_TASK_COHORT_TAIL |
					FLOWCAUSAL_TASK_PROPAGATE |
					FLOWCAUSAL_TASK_CONVERGE_TAIL |
					FLOWCAUSAL_TASK_BACKGROUND);
			ctx->flags |= FLOWCAUSAL_TASK_BPF_AUTO;
			if (ctx->confidence < policy->min_confidence)
				ctx->confidence = policy->min_confidence;
			ctx->last_update_ns = now;
			return;
		}
		ctx->flags |= FLOWCAUSAL_TASK_FRONTIER |
			      FLOWCAUSAL_TASK_COHORT_TAIL |
			      FLOWCAUSAL_TASK_PROPAGATE |
			      FLOWCAUSAL_TASK_BPF_AUTO |
			      FLOWCAUSAL_TASK_CONVERGE_TAIL;
		ctx->flags &= ~FLOWCAUSAL_TASK_BACKGROUND;
		if (ctx->confidence < default_bpf_auto_confidence)
			ctx->confidence = default_bpf_auto_confidence;
		if (ctx->confidence < policy->min_confidence)
			ctx->confidence = policy->min_confidence;
		ctx->last_update_ns = now;
		return;
	}
	if (default_respect_c_assist && (ctx->flags & FLOWCAUSAL_TASK_C_ASSIST))
		return;

	/* Keep the classification fresh on every enqueue/re-entry.  Exec clears the
	 * previously auto-derived state, so the next enqueue can reclassify from the
	 * new executable image and the current runtime/wait evidence.
	 */
	if (ctx->flags & FLOWCAUSAL_TASK_BPF_AUTO)
		clear_auto_flags(ctx);

	mode = auto_task_mode_hint(ctx, p);
	if (mode == FLOWCAUSAL_TASK_MODE_COMPUTE) {
		ctx->flags |= FLOWCAUSAL_TASK_FRONTIER |
			      FLOWCAUSAL_TASK_COHORT_TAIL |
			      FLOWCAUSAL_TASK_PROPAGATE |
			      FLOWCAUSAL_TASK_BPF_AUTO;
		if (ctx->confidence < default_bpf_auto_confidence)
			ctx->confidence = default_bpf_auto_confidence;
		if (ctx->confidence < policy->min_confidence)
			ctx->confidence = policy->min_confidence;
		ctx->last_update_ns = now;
		return;
	}

	if (mode == FLOWCAUSAL_TASK_MODE_INTERACTIVE) {
		ctx->flags |= FLOWCAUSAL_TASK_BPF_AUTO;
		if (ctx->confidence < policy->min_confidence)
			ctx->confidence = policy->min_confidence;
		ctx->last_update_ns = now;
		return;
	}

	if (ctx->runtime_ns >= SCX_SLICE_DFL) {
		ctx->flags |= FLOWCAUSAL_TASK_BACKGROUND | FLOWCAUSAL_TASK_BPF_AUTO;
		ctx->last_update_ns = now;
	}
}

static __always_inline void bump_global_stat(__u32 what, __u64 delta);

static __always_inline void bump_mode_stat(__u32 mode, bool enqueue, bool dispatch)
{
	__u32 stat = 0;

	if (!mode || (!enqueue && !dispatch))
		return;

	if (enqueue) {
		if (mode == FLOWCAUSAL_TASK_MODE_INTERACTIVE)
			stat = 43;
		else if (mode == FLOWCAUSAL_TASK_MODE_COMPUTE)
			stat = 44;
		else if (mode == FLOWCAUSAL_TASK_MODE_TAIL)
			stat = 45;
		else if (mode == FLOWCAUSAL_TASK_MODE_BACKGROUND)
			stat = 46;
	} else if (dispatch) {
		if (mode == FLOWCAUSAL_TASK_MODE_INTERACTIVE)
			stat = 47;
		else if (mode == FLOWCAUSAL_TASK_MODE_COMPUTE)
			stat = 48;
		else if (mode == FLOWCAUSAL_TASK_MODE_TAIL)
			stat = 49;
		else if (mode == FLOWCAUSAL_TASK_MODE_BACKGROUND)
			stat = 50;
	}

	if (stat)
		bump_global_stat(stat, 1);
}

static __always_inline void emit_event(__u32 kind, struct task_struct *p,
				       struct flow_task_ctx *ctx, __u32 flags)
{
	struct flow_event *e;
	__u64 pid_tgid = bpf_get_current_pid_tgid();
	__u32 bit;

	if (default_control_event_mask) {
		bit = 1U << kind;
		if (!(default_control_event_mask & bit))
			return;
	}

	e = bpf_ringbuf_reserve(&events, sizeof(*e), 0);
	if (!e)
		return;

	e->ts_ns = bpf_ktime_get_ns();
	e->session_id = ctx ? ctx->session_id : 0;
	e->stage_id = ctx ? ctx->stage_id : 0;
	e->cohort_id = ctx ? ctx->cohort_id : 0;
	if (p) {
		e->pid = p->pid;
		e->tgid = p->tgid;
		bpf_probe_read_kernel_str(e->comm, sizeof(e->comm), p->comm);
	} else {
		e->pid = (__u32)pid_tgid;
		e->tgid = (__u32)(pid_tgid >> 32);
		bpf_get_current_comm(e->comm, sizeof(e->comm));
	}
	e->ppid = ctx ? ctx->parent_pid : 0;
	e->kind = kind;
	e->flags = flags;
	e->cpu = bpf_get_smp_processor_id();
	bpf_ringbuf_submit(e, 0);
}

static __always_inline struct flow_session_stats *
lookup_or_create_session_stats(__u64 session_id)
{
	struct flow_session_stats zero = {};
	struct flow_session_stats *s;

	if (!session_id)
		return NULL;

	s = bpf_map_lookup_elem(&session_stats, &session_id);
	if (!s) {
		zero.last_event_ns = bpf_ktime_get_ns();
		bpf_map_update_elem(&session_stats, &session_id, &zero, BPF_NOEXIST);
		s = bpf_map_lookup_elem(&session_stats, &session_id);
	}
	return s;
}

static __always_inline struct flow_session_runtime *
lookup_or_create_session_runtime(__u64 session_id)
{
	struct flow_session_runtime zero = {};
	struct flow_session_runtime *s;

	if (!session_id)
		return NULL;

	s = bpf_map_lookup_elem(&session_runtime, &session_id);
	if (!s) {
		bpf_map_update_elem(&session_runtime, &session_id, &zero,
				    BPF_NOEXIST);
		s = bpf_map_lookup_elem(&session_runtime, &session_id);
	}
	return s;
}

static __always_inline void mark_package_critical_done(__u64 session_id)
{
	struct flow_session_runtime *s;

	if (!FLOWCAUSAL_DEFAULT_PARENT_EXIT_CLEANUP_PACKAGE_AFTER_CRITICAL)
		return;
	s = lookup_or_create_session_runtime(session_id);
	if (!s)
		return;
	s->critical_child_done_mask |= FLOWCAUSAL_RUNTIME_PACKAGE_CRITICAL_DONE;
}

static __always_inline bool package_critical_done(__u64 session_id)
{
	struct flow_session_runtime *s;

	if (!FLOWCAUSAL_DEFAULT_PARENT_EXIT_CLEANUP_PACKAGE_AFTER_CRITICAL)
		return false;
	s = bpf_map_lookup_elem(&session_runtime, &session_id);
	if (!s)
		return false;
	return s->critical_child_done_mask & FLOWCAUSAL_RUNTIME_PACKAGE_CRITICAL_DONE;
}

static __always_inline bool package_postcrit_cleanup_used(__u64 session_id)
{
	struct flow_session_runtime *s;

	if (!FLOWCAUSAL_DEFAULT_PARENT_EXIT_CLEANUP_PACKAGE_AFTER_CRITICAL_ONESHOT)
		return false;
	s = bpf_map_lookup_elem(&session_runtime, &session_id);
	if (!s)
		return false;
	return s->critical_child_done_mask &
	       FLOWCAUSAL_RUNTIME_PACKAGE_POSTCRIT_CLEANUP_USED;
}

static __always_inline void mark_package_postcrit_cleanup_used(__u64 session_id)
{
	struct flow_session_runtime *s;

	if (!FLOWCAUSAL_DEFAULT_PARENT_EXIT_CLEANUP_PACKAGE_AFTER_CRITICAL_ONESHOT)
		return;
	s = lookup_or_create_session_runtime(session_id);
	if (!s)
		return;
	s->critical_child_done_mask |=
		FLOWCAUSAL_RUNTIME_PACKAGE_POSTCRIT_CLEANUP_USED;
}

static __always_inline void mark_session_queued(struct task_struct *p,
						struct flow_task_ctx *ctx)
{
	struct flow_sched_task_state *state;
	struct flow_session_runtime *s;

	if (!default_converge || !p || !ctx || !ctx->session_id)
		return;

	state = bpf_task_storage_get(&sched_task_state, p, 0,
				     BPF_LOCAL_STORAGE_GET_F_CREATE);
	if (!state)
		return;
	if (state->queued)
		return;

	s = lookup_or_create_session_runtime(ctx->session_id);
	if (!s)
		return;
	state->queued = true;
	__sync_fetch_and_add(&s->runnable_tasks, 1);
}

static __always_inline void mark_session_dequeued(struct task_struct *p,
						  struct flow_task_ctx *ctx)
{
	struct flow_sched_task_state *state;
	struct flow_session_runtime *s;

	if (!default_converge || !p || !ctx || !ctx->session_id)
		return;

	state = bpf_task_storage_get(&sched_task_state, p, 0, 0);
	if (!state || !state->queued)
		return;

	s = bpf_map_lookup_elem(&session_runtime, &ctx->session_id);
	if (s && s->runnable_tasks)
		__sync_fetch_and_sub(&s->runnable_tasks, 1);
	state->queued = false;
}

static __always_inline void bump_session_stat(__u64 session_id, __u32 what,
						      __u64 delta)
{
	struct flow_session_stats *s;

	s = lookup_or_create_session_stats(session_id);
	if (!s)
		return;

	if (what == 0)
		__sync_fetch_and_add(&s->seen_enqueues, delta);
	else if (what == 1)
		__sync_fetch_and_add(&s->frontier_enqueues, delta);
	else if (what == 2)
		__sync_fetch_and_add(&s->fallback_enqueues, delta);
	else if (what == 3)
		__sync_fetch_and_add(&s->width_held_enqueues, delta);
	else if (what == 4)
		__sync_fetch_and_add(&s->total_runnable_wait_ns, delta);
	else if (what == 5)
		__sync_fetch_and_add(&s->frontier_runnable_wait_ns, delta);
	else if (what == 6)
		__sync_fetch_and_add(&s->total_runtime_ns, delta);
	else if (what == 7)
		__sync_fetch_and_add(&s->frontier_runtime_ns, delta);
	else if (what == 8)
		__sync_fetch_and_add(&s->frontier_width_releases, delta);
	s->last_event_ns = bpf_ktime_get_ns();
}

static __always_inline void bump_global_stat(__u32 what, __u64 delta)
{
	__u32 key = 0;
	struct flow_global_stats *s;

	s = bpf_map_lookup_elem(&global_stats, &key);
	if (!s)
		return;

	if (what == 0)
		__sync_fetch_and_add(&s->frontier_dispatches, delta);
	else if (what == 1)
		__sync_fetch_and_add(&s->shared_dispatches, delta);
	else if (what == 2)
		__sync_fetch_and_add(&s->shared_first_dispatches, delta);
	else if (what == 3)
		__sync_fetch_and_add(&s->frontier_guard_trips, delta);
	else if (what == 4)
		__sync_fetch_and_add(&s->frontier_guard_misses, delta);
	else if (what == 5)
		__sync_fetch_and_add(&s->shared_cooldown_entries, delta);
	else if (what == 6)
		__sync_fetch_and_add(&s->shared_cooldown_dispatches, delta);
	else if (what == 7)
		__sync_fetch_and_add(&s->shared_cooldown_misses, delta);
	else if (what == 8)
		__sync_fetch_and_add(&s->pressure_fallback_entries, delta);
	else if (what == 9)
		__sync_fetch_and_add(&s->pressure_fallback_enqueues, delta);
	else if (what == 10)
		__sync_fetch_and_add(&s->frontier_budget_windows, delta);
	else if (what == 11)
		__sync_fetch_and_add(&s->frontier_budget_trips, delta);
	else if (what == 12)
		__sync_fetch_and_add(&s->frontier_budget_shared_dispatches, delta);
	else if (what == 13)
		__sync_fetch_and_add(&s->frontier_budget_misses, delta);
	else if (what == 14)
		__sync_fetch_and_add(&s->shared_wait_pressure_events, delta);
	else if (what == 15)
		__sync_fetch_and_add(&s->shared_wait_pressure_wait_ns, delta);
	else if (what == 16)
		__sync_fetch_and_add(&s->pressure_dispatch_entries, delta);
	else if (what == 17)
		__sync_fetch_and_add(&s->pressure_dispatches, delta);
	else if (what == 18)
		__sync_fetch_and_add(&s->pressure_dispatch_misses, delta);
	else if (what == 19)
		__sync_fetch_and_add(&s->shared_wakeup_preempt_entries, delta);
	else if (what == 20)
		__sync_fetch_and_add(&s->shared_wakeup_preempt_dispatches, delta);
	else if (what == 21)
		__sync_fetch_and_add(&s->shared_wakeup_preempt_misses, delta);
	else if (what == 22)
		__sync_fetch_and_add(&s->shared_local_enqueues, delta);
	else if (what == 23)
		__sync_fetch_and_add(&s->shared_percpu_enqueues, delta);
	else if (what == 24)
		__sync_fetch_and_add(&s->shared_percpu_dispatches, delta);
	else if (what == 25)
		__sync_fetch_and_add(&s->shared_percpu_misses, delta);
	else if (what == 26)
		__sync_fetch_and_add(&s->frontier_wakeup_preempt_entries, delta);
	else if (what == 27)
		__sync_fetch_and_add(&s->background_enqueues, delta);
	else if (what == 28)
		__sync_fetch_and_add(&s->background_dispatches, delta);
	else if (what == 29)
		__sync_fetch_and_add(&s->frontier_dsq_enqueues, delta);
	else if (what == 30)
		__sync_fetch_and_add(&s->tracked_shared_path_enqueues, delta);
	else if (what == 31)
		__sync_fetch_and_add(&s->untracked_shared_path_enqueues, delta);
	else if (what == 32)
		__sync_fetch_and_add(&s->shared_global_enqueues, delta);
	else if (what == 33)
		__sync_fetch_and_add(&s->shared_global_tracked_enqueues, delta);
	else if (what == 34)
		__sync_fetch_and_add(&s->shared_global_untracked_enqueues, delta);
	else if (what == 35)
		__sync_fetch_and_add(&s->latency_dsq_enqueues, delta);
	else if (what == 36)
		__sync_fetch_and_add(&s->shared_local_tracked_enqueues, delta);
	else if (what == 37)
		__sync_fetch_and_add(&s->shared_local_untracked_enqueues, delta);
	else if (what == 38)
		__sync_fetch_and_add(&s->shared_percpu_tracked_enqueues, delta);
	else if (what == 39)
		__sync_fetch_and_add(&s->shared_percpu_untracked_enqueues, delta);
	else if (what == 40)
		__sync_fetch_and_add(&s->shared_local_pick_fallbacks, delta);
	else if (what == 41)
		__sync_fetch_and_add(&s->shared_percpu_pick_fallbacks, delta);
	else if (what == 42)
		__sync_fetch_and_add(&s->shared_local_frontier_fallback_enqueues,
				     delta);
	else if (what == 43)
		__sync_fetch_and_add(&s->mode_interactive_enqueues, delta);
	else if (what == 44)
		__sync_fetch_and_add(&s->mode_compute_enqueues, delta);
	else if (what == 45)
		__sync_fetch_and_add(&s->mode_tail_enqueues, delta);
	else if (what == 46)
		__sync_fetch_and_add(&s->mode_background_enqueues, delta);
	else if (what == 47)
		__sync_fetch_and_add(&s->mode_interactive_dispatches, delta);
	else if (what == 48)
		__sync_fetch_and_add(&s->mode_compute_dispatches, delta);
	else if (what == 49)
		__sync_fetch_and_add(&s->mode_tail_dispatches, delta);
	else if (what == 50)
		__sync_fetch_and_add(&s->mode_background_dispatches, delta);
	else if (what == 51)
		__sync_fetch_and_add(&s->converge_dsq_enqueues, delta);
	else if (what == 52)
		__sync_fetch_and_add(&s->converge_dispatches, delta);
	else if (what == 53)
		__sync_fetch_and_add(&s->session_frontier_dsq_enqueues, delta);
	else if (what == 54)
		__sync_fetch_and_add(&s->session_frontier_dispatches, delta);
	else if (what == 55)
		__sync_fetch_and_add(&s->adaptive_width_tail_admissions, delta);
	else if (what == 56)
		__sync_fetch_and_add(&s->adaptive_width_wide_admissions, delta);
	else if (what == 57)
		__sync_fetch_and_add(&s->adaptive_width_held_enqueues, delta);
	else if (what == 58)
		__sync_fetch_and_add(&s->root_copy_dsq_enqueues, delta);
	else if (what == 59)
		__sync_fetch_and_add(&s->root_copy_dispatches, delta);
}

static __always_inline bool frontier_dispatch_budget_enabled(void)
{
	return default_frontier_dispatch_window_ns &&
	       default_frontier_dispatch_window_budget;
}

static __always_inline __u64 shared_slice_ns(void)
{
	if (default_shared_slice_ns)
		return default_shared_slice_ns;
	return SCX_SLICE_DFL;
}

static __always_inline __u64 shared_wakeup_slice_ns(void)
{
	if (default_shared_wakeup_slice_ns)
		return default_shared_wakeup_slice_ns;
	return shared_slice_ns();
}

static __always_inline void remember_task_slice(struct task_struct *p,
						__u64 slice_ns)
{
	struct flow_sched_task_state *state;

	if (!p)
		return;

	state = bpf_task_storage_get(&sched_task_state, p, 0,
				     BPF_LOCAL_STORAGE_GET_F_CREATE);
	if (!state)
		return;

	state->last_slice_ns = slice_ns ? slice_ns : SCX_SLICE_DFL;
}

static __always_inline void remember_task_cpu(struct task_struct *p, s32 cpu)
{
	struct flow_sched_task_state *state;

	if (!p)
		return;

	state = bpf_task_storage_get(&sched_task_state, p, 0,
				     BPF_LOCAL_STORAGE_GET_F_CREATE);
	if (!state)
		return;

	state->selected_cpu = cpu;
}

static __always_inline s32 remembered_task_cpu(struct task_struct *p)
{
	struct flow_sched_task_state *state;

	state = bpf_task_storage_get(&sched_task_state, p, 0, 0);
	if (!state)
		return -1;
	return state->selected_cpu;
}

static __always_inline s32 flowcausal_nth_allowed_cpu(struct task_struct *p,
						      __u32 ordinal)
{
	__u32 nr_cpu_ids;
	__u32 seen = 0;
	__u32 cpu;
	s32 last = -1;

	if (!p)
		return -1;

	nr_cpu_ids = scx_bpf_nr_cpu_ids();
	if (nr_cpu_ids > FLOWCAUSAL_DEFAULT_CPUSET_LANE_SCAN_CPUS)
		nr_cpu_ids = FLOWCAUSAL_DEFAULT_CPUSET_LANE_SCAN_CPUS;

	bpf_for(cpu, 0, nr_cpu_ids) {
		if (!bpf_cpumask_test_cpu(cpu, p->cpus_ptr))
			continue;
		last = (s32)cpu;
		if (seen == ordinal)
			return (s32)cpu;
		seen++;
	}

	return last;
}

static __always_inline s32 flowcausal_allowed_cpu_ordinal(struct task_struct *p,
							  s32 target_cpu)
{
	__u32 nr_cpu_ids;
	__u32 seen = 0;
	__u32 cpu;

	if (!p || target_cpu < 0)
		return -1;
	nr_cpu_ids = scx_bpf_nr_cpu_ids();
	if (nr_cpu_ids > FLOWCAUSAL_DEFAULT_CPUSET_LANE_SCAN_CPUS)
		nr_cpu_ids = FLOWCAUSAL_DEFAULT_CPUSET_LANE_SCAN_CPUS;
	if ((__u32)target_cpu >= nr_cpu_ids)
		return -1;
	if (FLOWCAUSAL_DEFAULT_CPUSET_LANE_ZERO_PREFIX_FASTPATH &&
	    bpf_cpumask_test_cpu(0, p->cpus_ptr) &&
	    bpf_cpumask_test_cpu((__u32)target_cpu, p->cpus_ptr))
		return target_cpu;

	bpf_for(cpu, 0, nr_cpu_ids) {
		if (!bpf_cpumask_test_cpu(cpu, p->cpus_ptr))
			continue;
		if ((s32)cpu == target_cpu)
			return (s32)seen;
		seen++;
	}

	return -1;
}

static __always_inline s32 flowcausal_lane_cpu(struct task_struct *p,
					       s32 logical_cpu)
{
	s32 cpu;

	if (logical_cpu < 0)
		return logical_cpu;
	if (!FLOWCAUSAL_DEFAULT_CPUSET_LANE_MAP)
		return logical_cpu;
	if ((__u32)logical_cpu < scx_bpf_nr_cpu_ids() &&
	    bpf_cpumask_test_cpu((__u32)logical_cpu, p->cpus_ptr))
		return logical_cpu;

	cpu = flowcausal_nth_allowed_cpu(p, (__u32)logical_cpu);
	if (cpu >= 0)
		return cpu;
	return logical_cpu;
}

static __always_inline s32 flowcausal_span_candidate_cpu(struct task_struct *p,
							 s32 logical_anchor_cpu,
							 __u32 idx)
{
	if (!FLOWCAUSAL_DEFAULT_CPUSET_LANE_MAP)
		return logical_anchor_cpu + (s32)idx;
	return flowcausal_lane_cpu(p, logical_anchor_cpu + (s32)idx);
}

static __always_inline bool last_stop_was_blocked(struct task_struct *p)
{
	struct flow_sched_task_state *state;

	state = bpf_task_storage_get(&sched_task_state, p, 0, 0);
	if (!state)
		return false;
	return state->last_blocked;
}

static __always_inline bool blocked_long_enough_for_shared_wakeup(struct task_struct *p,
								  __u64 now)
{
	struct flow_sched_task_state *state;
	__u64 blocked_ns;

	if (!default_shared_wakeup_min_block_ns)
		return true;

	state = bpf_task_storage_get(&sched_task_state, p, 0, 0);
	if (!state)
		return false;
	if (!state->blocked_since_ns)
		return false;
	if (!time_after(now, state->blocked_since_ns))
		return false;

	blocked_ns = now - state->blocked_since_ns;
	return blocked_ns >= default_shared_wakeup_min_block_ns;
}

static __always_inline __u64 frontier_wakeup_preempt_flags(struct task_struct *p,
							   __u64 enq_flags)
{
	if (!default_frontier_wakeup_preempt)
		return enq_flags;
	if (!last_stop_was_blocked(p))
		return enq_flags;
	if (scx_bpf_task_running(p))
		return enq_flags;

	bump_global_stat(26, 1);
	scx_bpf_kick_cpu(scx_bpf_task_cpu(p), SCX_KICK_PREEMPT);
	return enq_flags | SCX_ENQ_PREEMPT;
}

static __always_inline bool shared_wakeup_preempt_candidate(struct task_struct *p,
							    bool allow_preempt,
							    __u64 now)
{
	if (!default_shared_wakeup_preempt)
		return false;
	if (!allow_preempt)
		return false;
	if (!last_stop_was_blocked(p))
		return false;
	if (!blocked_long_enough_for_shared_wakeup(p, now))
		return false;
	if (scx_bpf_task_running(p))
		return false;

	return true;
}

static __always_inline __u64 shared_wakeup_preempt_flags(struct task_struct *p,
							 __u64 enq_flags)
{
	/*
	 * SCX_ENQ_PREEMPT and a CPU kick are not sufficient by themselves if the
	 * next dispatch still probes the frontier DSQ before the shared DSQ.  Keep
	 * a tiny best-effort token so the preempted CPU gives the just-woken
	 * latency DSQ one shared-first dispatch opportunity.
	 */
	shared_wakeup_preempt_dispatch_pending = true;
	bump_global_stat(19, 1);
	scx_bpf_kick_cpu(scx_bpf_task_cpu(p), SCX_KICK_PREEMPT);
	return enq_flags | SCX_ENQ_PREEMPT;
}

static __always_inline __u64 shared_enqueue_flags(struct task_struct *p,
						  __u64 enq_flags,
						  bool allow_preempt,
						  __u64 now)
{
	if (!shared_wakeup_preempt_candidate(p, allow_preempt, now))
		return enq_flags;
	return shared_wakeup_preempt_flags(p, enq_flags);
}

static __always_inline __u64 parent_exit_latency_slice_ns(void)
{
	if (FLOWCAUSAL_DEFAULT_PARENT_EXIT_LATENCY_SLICE_NS)
		return FLOWCAUSAL_DEFAULT_PARENT_EXIT_LATENCY_SLICE_NS;
	return shared_slice_ns();
}

static __always_inline void consume_parent_cleanup_pulse(struct flow_task_ctx *ctx)
{
	if (!ctx)
		return;
	if (FLOWCAUSAL_DEFAULT_PARENT_EXIT_CLEANUP_ONESHOT) {
		ctx->flags &= ~FLOWCAUSAL_TASK_PARENT_LATENCY;
		ctx->stage_id = 0;
		return;
	}
	if (!FLOWCAUSAL_DEFAULT_PARENT_EXIT_CLEANUP_PULSE_LIMIT &&
	    !ctx->propagation_depth)
		return;
	if (ctx->propagation_depth <= 1) {
		ctx->propagation_depth = 0;
		ctx->flags &= ~FLOWCAUSAL_TASK_PARENT_LATENCY;
		ctx->stage_id = 0;
		return;
	}
	ctx->propagation_depth--;
}

static __always_inline bool consume_parent_exit_latency(struct task_struct *p,
							struct flow_task_key *key,
							struct flow_task_ctx *ctx,
							__u64 enq_flags,
							__u64 vtime)
{
	__u64 slice;
	__u64 flags;
	__u64 now;

	if (!FLOWCAUSAL_DEFAULT_PARENT_EXIT_LATENCY)
		return false;
	if (!p || !key || !ctx)
		return false;
	if (ctx->session_id != FLOWCAUSAL_PARENT_EXIT_SESSION_ID)
		return false;
	if (!(ctx->flags & FLOWCAUSAL_TASK_PARENT_LATENCY))
		return false;
	now = bpf_ktime_get_ns();
	if (FLOWCAUSAL_DEFAULT_PARENT_EXIT_LATENCY_RESCUE_ONLY &&
	    !package_rescue_active_now(now))
		return false;
	if (FLOWCAUSAL_DEFAULT_PARENT_EXIT_CLEANUP &&
	    ctx->stage_id && time_after(now, ctx->stage_id)) {
		ctx->flags &= ~FLOWCAUSAL_TASK_PARENT_LATENCY;
		return false;
	}

	slice = parent_exit_latency_slice_ns();
	if (FLOWCAUSAL_DEFAULT_PACKAGE_BG_PARENT_CLEANUP_SLICE_NS &&
	    (ctx->flags & FLOWCAUSAL_TASK_STAGE_PACKAGE_BG))
		slice = FLOWCAUSAL_DEFAULT_PACKAGE_BG_PARENT_CLEANUP_SLICE_NS;
	flags = enq_flags;
	if (FLOWCAUSAL_DEFAULT_PARENT_EXIT_LATENCY_PREEMPT)
		flags |= SCX_ENQ_PREEMPT;
	if (FLOWCAUSAL_DEFAULT_PARENT_EXIT_CLEANUP &&
	    FLOWCAUSAL_DEFAULT_PARENT_EXIT_CLEANUP_CPU >= 0) {
		s32 cpu = (s32)FLOWCAUSAL_DEFAULT_PARENT_EXIT_CLEANUP_CPU;

		if (FLOWCAUSAL_DEFAULT_PACKAGE_BG_PARENT_CLEANUP_CPU >= 0 &&
		    ctx->anchor_cpu >= 0 &&
		    (__u32)ctx->anchor_cpu < scx_bpf_nr_cpu_ids())
			cpu = ctx->anchor_cpu;
		if (FLOWCAUSAL_DEFAULT_PARENT_EXIT_CLEANUP_CHILD_CPU &&
		    ctx->anchor_cpu >= 0 &&
		    (__u32)ctx->anchor_cpu < scx_bpf_nr_cpu_ids())
			cpu = ctx->anchor_cpu;
		cpu = flowcausal_lane_cpu(p, cpu);
		if ((__u32)cpu < scx_bpf_nr_cpu_ids() &&
		    bpf_cpumask_test_cpu((__u32)cpu, p->cpus_ptr)) {
			remember_task_cpu(p, cpu);
			remember_task_slice(p, slice);
			scx_bpf_kick_cpu(cpu,
					 FLOWCAUSAL_DEFAULT_PARENT_EXIT_LATENCY_PREEMPT ?
					 SCX_KICK_PREEMPT : SCX_KICK_IDLE);
			if (FLOWCAUSAL_DEFAULT_PARENT_EXIT_CLEANUP_DSQ)
				scx_bpf_dsq_insert(p, FLOWCAUSAL_CLEANUP_DSQ,
						   slice, flags);
			else
				scx_bpf_dsq_insert(p, SCX_DSQ_LOCAL_ON | (__u64)cpu,
						   slice, flags);
			bump_global_stat(FLOW_GSTAT_LATENCY_DSQ_ENQUEUES, 1);
			bump_mode_stat(FLOWCAUSAL_TASK_MODE_INTERACTIVE, true, false);
			consume_parent_cleanup_pulse(ctx);
			return true;
		}
	}
	remember_task_slice(p, slice);
	scx_bpf_kick_cpu(scx_bpf_task_cpu(p),
			 FLOWCAUSAL_DEFAULT_PARENT_EXIT_LATENCY_PREEMPT ?
			 SCX_KICK_PREEMPT : SCX_KICK_IDLE);
	if (fifo_sched)
		scx_bpf_dsq_insert(p, FLOWCAUSAL_LATENCY_DSQ, slice, flags);
	else
		scx_bpf_dsq_insert_vtime(p, FLOWCAUSAL_LATENCY_DSQ, slice,
					 vtime, flags);
	bump_global_stat(FLOW_GSTAT_LATENCY_DSQ_ENQUEUES, 1);
	bump_mode_stat(FLOWCAUSAL_TASK_MODE_INTERACTIVE, true, false);
	consume_parent_cleanup_pulse(ctx);
	if (!FLOWCAUSAL_DEFAULT_PARENT_EXIT_CLEANUP)
		bpf_map_delete_elem(&task_ctx, key);
	return true;
}

static __always_inline bool parent_exit_cleanup_root_direct_allowed(struct flow_task_ctx *ctx)
{
	struct flow_task_key parent_key = {};
	struct flow_task_ctx *parent;

	if (!FLOWCAUSAL_DEFAULT_PARENT_EXIT_CLEANUP_ROOT_DIRECT_ONLY)
		return true;
	if (!ctx)
		return false;
	if (FLOWCAUSAL_DEFAULT_PARENT_EXIT_CLEANUP_ROOT_DIRECT_PACKAGE_ALL &&
	    ctx->anchor_cpu == 5)
		return true;
	if (ctx->flags & FLOWCAUSAL_TASK_ROOT)
		return true;
	if (!ctx->parent_pid)
		return false;
	parent_key.pid = ctx->parent_pid;
	parent = bpf_map_lookup_elem(&task_ctx, &parent_key);
	if (!parent)
		return false;
	return parent->flags & FLOWCAUSAL_TASK_ROOT;
}

static __always_inline bool parent_exit_cleanup_package_internal_pulse_allowed(struct flow_task_ctx *ctx)
{
	if (!FLOWCAUSAL_DEFAULT_PARENT_EXIT_CLEANUP_PACKAGE_INTERNAL_PULSE)
		return false;
	if (!ctx)
		return false;
	if (ctx->anchor_cpu != 5)
		return false;
	if (FLOWCAUSAL_DEFAULT_PARENT_EXIT_CLEANUP_PACKAGE_BG_MARK_ONLY &&
	    !(ctx->flags & FLOWCAUSAL_TASK_PACKAGE_BG_MARK))
		return false;
	if (ctx->flags & FLOWCAUSAL_TASK_ROOT)
		return false;
	return true;
}

static __always_inline bool parent_exit_cleanup_package_after_critical_allowed(struct flow_task_ctx *ctx)
{
	if (!FLOWCAUSAL_DEFAULT_PARENT_EXIT_CLEANUP_PACKAGE_AFTER_CRITICAL)
		return false;
	if (!ctx)
		return false;
	if (ctx->anchor_cpu != 5)
		return false;
	if (ctx->flags & FLOWCAUSAL_TASK_ROOT)
		return false;
	if (package_postcrit_cleanup_used(ctx->session_id))
		return false;
	return package_critical_done(ctx->session_id);
}

static __always_inline bool parent_exit_cleanup_admits(struct flow_task_ctx *ctx)
{
	bool root_direct_allowed;
	bool package_after_critical_allowed;

	if (!FLOWCAUSAL_DEFAULT_PARENT_EXIT_CLEANUP)
		return true;
	if (!ctx)
		return false;
	if (FLOWCAUSAL_DEFAULT_PARENT_EXIT_CLEANUP_SKIP_PY &&
	    ctx->anchor_cpu == 4)
		return false;
	if (FLOWCAUSAL_DEFAULT_PARENT_EXIT_CLEANUP_SKIP_WHILE_PY_TEST_ACTIVE &&
	    ctx->anchor_cpu != 4 &&
	    py_test_cleanup_guard_active_now(bpf_ktime_get_ns()))
		return false;
	if (FLOWCAUSAL_DEFAULT_PARENT_EXIT_CLEANUP_SKIP_WHILE_PY_CRITICAL_ACTIVE &&
	    ctx->anchor_cpu != 4 &&
	    py_critical_cleanup_guard_active())
		return false;
	root_direct_allowed = parent_exit_cleanup_root_direct_allowed(ctx);
	package_after_critical_allowed =
		parent_exit_cleanup_package_after_critical_allowed(ctx);
	if (!root_direct_allowed &&
	    !package_after_critical_allowed &&
	    !parent_exit_cleanup_package_internal_pulse_allowed(ctx))
		return false;
	if (FLOWCAUSAL_DEFAULT_PARENT_EXIT_CLEANUP_CXX_ONLY_STRICT &&
	    FLOWCAUSAL_DEFAULT_PARENT_EXIT_CLEANUP_CXX_ONLY)
		return ctx->anchor_cpu == 0;
	if (FLOWCAUSAL_DEFAULT_PARENT_EXIT_CLEANUP_SELECTED_ONLY) {
		if (FLOWCAUSAL_DEFAULT_PARENT_EXIT_CLEANUP_CXX_ONLY &&
		    ctx->anchor_cpu == 0)
			return true;
		if (FLOWCAUSAL_DEFAULT_PACKAGE_BG_PARENT_CLEANUP &&
		    ctx->anchor_cpu == 5 &&
		    (ctx->flags & FLOWCAUSAL_TASK_STAGE_PACKAGE_BG))
			return true;
		if (FLOWCAUSAL_DEFAULT_PACKAGE_ROOT_PARENT_CLEANUP &&
		    ctx->anchor_cpu == 5 &&
		    (ctx->flags & FLOWCAUSAL_TASK_ROOT))
			return true;
		return false;
	}
	if (!FLOWCAUSAL_DEFAULT_PARENT_EXIT_CLEANUP_SKIP_PACKAGE)
		return true;
	if (FLOWCAUSAL_DEFAULT_PACKAGE_BG_PARENT_CLEANUP &&
	    ctx->anchor_cpu == 5 &&
	    (ctx->flags & FLOWCAUSAL_TASK_STAGE_PACKAGE_BG))
		return true;
	if (FLOWCAUSAL_DEFAULT_PACKAGE_ROOT_PARENT_CLEANUP &&
	    ctx->anchor_cpu == 5 &&
	    (ctx->flags & FLOWCAUSAL_TASK_ROOT))
		return true;
	if (FLOWCAUSAL_DEFAULT_PARENT_EXIT_CLEANUP_CXX_ONLY)
		return ctx->anchor_cpu == 0;
	return ctx->anchor_cpu != 5;
}

static __always_inline __u32 parent_exit_cleanup_pulse_limit_for_task(struct flow_task_ctx *ctx)
{
	if (parent_exit_cleanup_package_after_critical_allowed(ctx))
		return FLOWCAUSAL_DEFAULT_PARENT_EXIT_CLEANUP_PULSE_LIMIT;
	if (FLOWCAUSAL_DEFAULT_PARENT_EXIT_CLEANUP_PACKAGE_INTERNAL_PULSE &&
	    parent_exit_cleanup_package_internal_pulse_allowed(ctx) &&
	    !parent_exit_cleanup_root_direct_allowed(ctx))
		return FLOWCAUSAL_DEFAULT_PARENT_EXIT_CLEANUP_PACKAGE_INTERNAL_PULSE_LIMIT;
	return FLOWCAUSAL_DEFAULT_PARENT_EXIT_CLEANUP_PULSE_LIMIT;
}

static __always_inline __u64 parent_exit_cleanup_window_ns_for_task(struct flow_task_ctx *ctx)
{
	if (FLOWCAUSAL_DEFAULT_PARENT_EXIT_CLEANUP_PACKAGE_AFTER_CRITICAL_NS &&
	    parent_exit_cleanup_package_after_critical_allowed(ctx))
		return FLOWCAUSAL_DEFAULT_PARENT_EXIT_CLEANUP_PACKAGE_AFTER_CRITICAL_NS;
	return FLOWCAUSAL_DEFAULT_PARENT_EXIT_CLEANUP_NS;
}

static __always_inline void parent_exit_cleanup_mark_admitted(struct flow_task_ctx *ctx)
{
	if (parent_exit_cleanup_package_after_critical_allowed(ctx))
		mark_package_postcrit_cleanup_used(ctx->session_id);
}

static __always_inline bool valid_sched_cpu(s32 cpu)
{
	if (cpu < 0)
		return false;
	if ((__u32)cpu >= scx_bpf_nr_cpu_ids())
		return false;
	return true;
}

static __always_inline bool task_can_run_on_sched_cpu(struct task_struct *p,
						      s32 cpu)
{
	if (!valid_sched_cpu(cpu))
		return false;
	if (!bpf_cpumask_test_cpu((__u32)cpu, p->cpus_ptr))
		return false;
	return true;
}

static __always_inline bool package_rescue_active_now(__u64 now)
{
	if (!FLOWCAUSAL_DEFAULT_PACKAGE_RESCUE_AFTER_CXX_ROOT)
		return false;
	if (!package_rescue_until_ns)
		return false;
	return time_before(now, package_rescue_until_ns);
}

static __always_inline bool package_tail_locality_active_now(__u64 now)
{
	if (!FLOWCAUSAL_DEFAULT_PACKAGE_TAIL_LOCALITY_AFTER_PY_ROOT &&
	    !FLOWCAUSAL_DEFAULT_PACKAGE_TAIL_LOCALITY_AFTER_PY_TEST)
		return false;
	if (!package_tail_locality_until_ns)
		return false;
	return time_before(now, package_tail_locality_until_ns);
}

static __always_inline bool package_final_tail_active_now(__u64 now)
{
	if (!FLOWCAUSAL_DEFAULT_PACKAGE_FINAL_TAIL_AFTER_CXX_PY_ROOT &&
	    !FLOWCAUSAL_DEFAULT_PACKAGE_FINAL_TAIL_AFTER_CRITICAL_CHILDREN)
		return false;
	if (!package_final_tail_until_ns)
		return false;
	return time_before(now, package_final_tail_until_ns);
}

static __always_inline bool package_build_in_rescue(struct flow_task_ctx *ctx,
						    struct flow_session_policy *policy)
{
	if (!ctx || !policy)
		return false;
	if (policy->anchor_cpu != 5)
		return false;
	if (!(ctx->flags & FLOWCAUSAL_TASK_STAGE_BUILD_WIDE))
		return false;
	return package_rescue_active_now(bpf_ktime_get_ns());
}

static __always_inline bool package_rescue_all_locality_ctx(struct flow_task_ctx *ctx)
{
	__u64 now;

	if (!FLOWCAUSAL_DEFAULT_PACKAGE_RESCUE_ALL_LOCALITY &&
	    !FLOWCAUSAL_DEFAULT_PACKAGE_TAIL_LOCALITY_AFTER_PY_ROOT &&
	    !FLOWCAUSAL_DEFAULT_PACKAGE_TAIL_LOCALITY_AFTER_PY_TEST &&
	    !(FLOWCAUSAL_DEFAULT_PACKAGE_FINAL_TAIL_LOCALITY &&
	      (FLOWCAUSAL_DEFAULT_PACKAGE_FINAL_TAIL_AFTER_CXX_PY_ROOT ||
	       FLOWCAUSAL_DEFAULT_PACKAGE_FINAL_TAIL_AFTER_CRITICAL_CHILDREN)))
		return false;
	if (!ctx)
		return false;
	if (ctx->anchor_cpu != 5)
		return false;
	now = bpf_ktime_get_ns();
	if (FLOWCAUSAL_DEFAULT_PACKAGE_FINAL_TAIL_LOCALITY &&
	    package_final_tail_active_now(now))
		return true;
	if (FLOWCAUSAL_DEFAULT_PACKAGE_RESCUE_ALL_LOCALITY &&
	    package_rescue_active_now(now))
		return true;
	return package_tail_locality_active_now(now);
}

static __always_inline bool package_rescue_fallback_percpu_ctx(struct flow_task_ctx *ctx)
{
	__u64 now;

	if (!FLOWCAUSAL_DEFAULT_PACKAGE_RESCUE_FALLBACK_PERCPU &&
	    !FLOWCAUSAL_DEFAULT_PACKAGE_FINAL_TAIL_FALLBACK_PERCPU)
		return false;
	if (!ctx)
		return false;
	if (ctx->anchor_cpu != 5)
		return false;
	if (FLOWCAUSAL_DEFAULT_PACKAGE_RESCUE_FALLBACK_SKIP_ROOT &&
	    (ctx->flags & FLOWCAUSAL_TASK_ROOT))
		return false;
	if (FLOWCAUSAL_DEFAULT_PACKAGE_RESCUE_FALLBACK_ROOT_ONLY &&
	    !(ctx->flags & FLOWCAUSAL_TASK_ROOT))
		return false;
	if (FLOWCAUSAL_DEFAULT_PACKAGE_RESCUE_FALLBACK_ROOT_COPY_ONLY &&
	    (!(ctx->flags & FLOWCAUSAL_TASK_ROOT) ||
	     (ctx->flags & FLOWCAUSAL_TASK_ROOT_STAGE_STARTED)))
		return false;
	now = bpf_ktime_get_ns();
	if (FLOWCAUSAL_DEFAULT_PACKAGE_FINAL_TAIL_FALLBACK_PERCPU &&
	    package_final_tail_active_now(now))
		return true;
	if (FLOWCAUSAL_DEFAULT_PACKAGE_RESCUE_FALLBACK_PERCPU &&
	    package_rescue_active_now(now))
		return true;
	return false;
}

static __always_inline s32 package_rescue_fallback_cpu(struct task_struct *p,
						       struct flow_task_ctx *ctx,
						       s32 prev_cpu)
{
	bool final_tail = FLOWCAUSAL_DEFAULT_PACKAGE_FINAL_TAIL_FALLBACK_PERCPU &&
			  package_final_tail_active_now(bpf_ktime_get_ns());
	__u32 span = FLOWCAUSAL_DEFAULT_PACKAGE_RESCUE_SPAN ?
		     FLOWCAUSAL_DEFAULT_PACKAGE_RESCUE_SPAN :
		     (FLOWCAUSAL_DEFAULT_CXX_BUILD_SPAN ?
		      FLOWCAUSAL_DEFAULT_CXX_BUILD_SPAN : 3);
	s32 anchor_cpu = FLOWCAUSAL_DEFAULT_PACKAGE_RESCUE_ANCHOR_CPU >= 0 ?
			 FLOWCAUSAL_DEFAULT_PACKAGE_RESCUE_ANCHOR_CPU : 0;
	s32 remembered;
	__u32 off;
	__u32 i;

	if (!p || !ctx)
		return prev_cpu;
	if (final_tail) {
		span = FLOWCAUSAL_DEFAULT_PACKAGE_FINAL_TAIL_SPAN;
		anchor_cpu = FLOWCAUSAL_DEFAULT_PACKAGE_FINAL_TAIL_ANCHOR_CPU;
	}
	if (span > 8)
		span = 8;
	if (!span)
		return prev_cpu;

	remembered = remembered_task_cpu(p);
	if (FLOWCAUSAL_DEFAULT_CPUSET_LANE_MAP) {
		s32 remembered_logical = flowcausal_allowed_cpu_ordinal(p,
									remembered);

		if (remembered_logical >= anchor_cpu &&
		    (__u32)(remembered_logical - anchor_cpu) < span &&
		    task_can_run_on_sched_cpu(p, remembered))
			return remembered;
	} else {
		if (remembered >= anchor_cpu &&
		    (__u32)(remembered - anchor_cpu) < span &&
		    task_can_run_on_sched_cpu(p, remembered))
			return remembered;
	}

	off = (__u32)p->pid % span;
#pragma unroll
	for (i = 0; i < 8; i++) {
		__u32 idx;
		s32 cpu;

		if (i >= span)
			break;
		idx = off + i;
		if (idx >= span)
			idx -= span;
		cpu = flowcausal_span_candidate_cpu(p, anchor_cpu, idx);
		if (task_can_run_on_sched_cpu(p, cpu))
			return cpu;
	}
	anchor_cpu = flowcausal_lane_cpu(p, anchor_cpu);
	if (task_can_run_on_sched_cpu(p, anchor_cpu))
		return anchor_cpu;
	return prev_cpu;
}

static __always_inline bool root_copy_phase_shared(struct flow_task_ctx *ctx,
						   struct flow_session_policy *policy)
{
	if (!FLOWCAUSAL_DEFAULT_ROOT_COPY_SHARED &&
	    !FLOWCAUSAL_DEFAULT_PACKAGE_ROOT_COPY_SHARED)
		return false;
	if (!ctx)
		return false;
	if (!(ctx->flags & FLOWCAUSAL_TASK_ROOT))
		return false;
	if (ctx->flags & FLOWCAUSAL_TASK_ROOT_STAGE_STARTED)
		return false;
	if (FLOWCAUSAL_DEFAULT_PACKAGE_ROOT_COPY_SHARED &&
	    !FLOWCAUSAL_DEFAULT_ROOT_COPY_SHARED) {
		if (!policy || policy->anchor_cpu != 5)
			return false;
	}
	return true;
}

static __always_inline bool root_copy_phase_local(struct flow_task_ctx *ctx,
						  struct flow_session_policy *policy)
{
	if (!FLOWCAUSAL_DEFAULT_ROOT_COPY_LOCAL_DSQ &&
	    !FLOWCAUSAL_DEFAULT_PACKAGE_ROOT_COPY_LOCAL_DSQ &&
	    !FLOWCAUSAL_DEFAULT_CXX_ROOT_COPY_LOCAL_DSQ)
		return false;
	if (!ctx)
		return false;
	if (!(ctx->flags & FLOWCAUSAL_TASK_ROOT))
		return false;
	if (ctx->flags & FLOWCAUSAL_TASK_ROOT_STAGE_STARTED)
		return false;
	if (!FLOWCAUSAL_DEFAULT_ROOT_COPY_LOCAL_DSQ) {
		bool allowed = false;

		if (!policy)
			return false;
		if (FLOWCAUSAL_DEFAULT_PACKAGE_ROOT_COPY_LOCAL_DSQ &&
		    policy->anchor_cpu == 5)
			allowed = true;
		if (FLOWCAUSAL_DEFAULT_CXX_ROOT_COPY_LOCAL_DSQ &&
		    policy->anchor_cpu == 0)
			allowed = true;
		if (!allowed)
			return false;
	}
	return true;
}

static __always_inline bool package_root_copy_phase(struct flow_task_ctx *ctx,
						   struct flow_session_policy *policy)
{
	if (!ctx || !policy)
		return false;
	if (policy->anchor_cpu != 5)
		return false;
	if (!(ctx->flags & FLOWCAUSAL_TASK_ROOT))
		return false;
	if (ctx->flags & FLOWCAUSAL_TASK_ROOT_STAGE_STARTED)
		return false;
	return true;
}

static __always_inline bool root_copy_phase(struct flow_task_ctx *ctx)
{
	if (!ctx)
		return false;
	if (!(ctx->flags & FLOWCAUSAL_TASK_ROOT))
		return false;
	if (ctx->flags & FLOWCAUSAL_TASK_ROOT_STAGE_STARTED)
		return false;
	return true;
}

static __always_inline bool root_copy_single_owner_admits(struct flow_task_ctx *ctx)
{
	__u32 max_owners = FLOWCAUSAL_DEFAULT_ROOT_COPY_MAX_OWNERS;

	if (!FLOWCAUSAL_DEFAULT_ROOT_COPY_SINGLE_OWNER && !max_owners)
		return true;
	if (!ctx)
		return false;
	if (!(ctx->flags & FLOWCAUSAL_TASK_ROOT))
		return true;
	if (ctx->flags & FLOWCAUSAL_TASK_ROOT_STAGE_STARTED)
		return true;
	if (!ctx->session_id)
		return false;
	if (FLOWCAUSAL_DEFAULT_ROOT_COPY_SINGLE_OWNER)
		max_owners = 1;
	if (max_owners > 2)
		max_owners = 2;
	if (!root_copy_owner_session)
		root_copy_owner_session = ctx->session_id;
	if (root_copy_owner_session == ctx->session_id)
		return true;
	if (max_owners > 1) {
		if (!root_copy_owner_session2)
			root_copy_owner_session2 = ctx->session_id;
		if (root_copy_owner_session2 == ctx->session_id)
			return true;
	}
	return false;
}

static __always_inline void root_copy_single_owner_release(struct flow_task_ctx *ctx)
{
	if (!FLOWCAUSAL_DEFAULT_ROOT_COPY_SINGLE_OWNER &&
	    !FLOWCAUSAL_DEFAULT_ROOT_COPY_MAX_OWNERS)
		return;
	if (!ctx || !(ctx->flags & FLOWCAUSAL_TASK_ROOT))
		return;
	if (ctx->session_id && root_copy_owner_session == ctx->session_id)
		root_copy_owner_session = 0;
	if (ctx->session_id && root_copy_owner_session2 == ctx->session_id)
		root_copy_owner_session2 = 0;
}

static __always_inline bool root_copy_wakeup_preempt_candidate(struct task_struct *p,
							      struct flow_task_ctx *ctx,
							      __u64 now)
{
	struct flow_sched_task_state *state;
	__u64 blocked_ns;

	if (!FLOWCAUSAL_DEFAULT_ROOT_COPY_WAKEUP_PREEMPT)
		return false;
	if (!p || !root_copy_phase(ctx))
		return false;
	if (!last_stop_was_blocked(p))
		return false;
	if (scx_bpf_task_running(p))
		return false;
	state = bpf_task_storage_get(&sched_task_state, p, 0, 0);
	if (!state || !state->blocked_since_ns)
		return false;
	if (!time_after(now, state->blocked_since_ns))
		return false;
	blocked_ns = now - state->blocked_since_ns;
	if (blocked_ns < FLOWCAUSAL_DEFAULT_ROOT_COPY_WAKEUP_MIN_BLOCK_NS)
		return false;
	return true;
}

static __always_inline bool consume_root_copy_wakeup(struct task_struct *p,
						    struct flow_task_ctx *ctx,
						    __u64 enq_flags,
						    __u64 vtime,
						    __u64 now)
{
	__u64 slice;
	__u64 flags;

	if (!root_copy_wakeup_preempt_candidate(p, ctx, now))
		return false;

	slice = shared_wakeup_slice_ns();
	flags = enq_flags | SCX_ENQ_PREEMPT;
	remember_task_slice(p, slice);
	scx_bpf_kick_cpu(scx_bpf_task_cpu(p), SCX_KICK_PREEMPT);
	if (fifo_sched)
		scx_bpf_dsq_insert(p, FLOWCAUSAL_LATENCY_DSQ, slice, flags);
	else
		scx_bpf_dsq_insert_vtime(p, FLOWCAUSAL_LATENCY_DSQ,
					 slice, vtime, flags);
	bump_global_stat(FLOW_GSTAT_LATENCY_DSQ_ENQUEUES, 1);
	bump_global_stat(FLOW_GSTAT_SHARED_WAKEUP_PREEMPT_ENTRIES, 1);
	bump_mode_stat(FLOWCAUSAL_TASK_MODE_INTERACTIVE, true, false);
	return true;
}

static __always_inline __u64 tracked_shared_slice_ns(struct flow_task_ctx *ctx,
						     struct flow_session_policy *policy)
{
	if (FLOWCAUSAL_DEFAULT_PACKAGE_BG_RESCUE_SHARED_SLICE_NS &&
	    ctx && policy &&
	    policy->anchor_cpu == 5 &&
	    (ctx->flags & FLOWCAUSAL_TASK_STAGE_PACKAGE_BG) &&
	    package_rescue_active_now(bpf_ktime_get_ns()))
		return FLOWCAUSAL_DEFAULT_PACKAGE_BG_RESCUE_SHARED_SLICE_NS;
	if (FLOWCAUSAL_DEFAULT_PACKAGE_FINAL_TAIL_SHARED_SLICE_NS &&
	    ctx && policy &&
	    policy->anchor_cpu == 5 &&
	    package_final_tail_active_now(bpf_ktime_get_ns()))
		return FLOWCAUSAL_DEFAULT_PACKAGE_FINAL_TAIL_SHARED_SLICE_NS;
	if (FLOWCAUSAL_DEFAULT_ROOT_COPY_SHARED_SLICE_NS &&
	    ctx &&
	    (ctx->flags & FLOWCAUSAL_TASK_ROOT) &&
	    !(ctx->flags & FLOWCAUSAL_TASK_ROOT_STAGE_STARTED))
		return FLOWCAUSAL_DEFAULT_ROOT_COPY_SHARED_SLICE_NS;
	if (FLOWCAUSAL_DEFAULT_PACKAGE_ROOT_SHARED_SLICE_NS &&
	    ctx && policy &&
	    policy->anchor_cpu == 5 &&
	    (ctx->flags & FLOWCAUSAL_TASK_ROOT))
		return FLOWCAUSAL_DEFAULT_PACKAGE_ROOT_SHARED_SLICE_NS;
	return shared_slice_ns();
}

static __always_inline bool package_root_copy_wakeup_preempt_candidate(struct task_struct *p,
								       struct flow_task_ctx *ctx,
								       struct flow_session_policy *policy,
								       __u64 now)
{
	struct flow_sched_task_state *state;
	__u64 blocked_ns;

	if (!FLOWCAUSAL_DEFAULT_PACKAGE_ROOT_COPY_WAKEUP_PREEMPT)
		return false;
	if (!p || !package_root_copy_phase(ctx, policy))
		return false;
	if (!last_stop_was_blocked(p))
		return false;
	if (scx_bpf_task_running(p))
		return false;
	state = bpf_task_storage_get(&sched_task_state, p, 0, 0);
	if (!state || !state->blocked_since_ns)
		return false;
	if (!time_after(now, state->blocked_since_ns))
		return false;
	blocked_ns = now - state->blocked_since_ns;
	if (blocked_ns < FLOWCAUSAL_DEFAULT_PACKAGE_ROOT_COPY_WAKEUP_MIN_BLOCK_NS)
		return false;
	return true;
}

static __always_inline bool consume_package_root_copy_wakeup(struct task_struct *p,
							    struct flow_task_ctx *ctx,
							    struct flow_session_policy *policy,
							    __u64 enq_flags,
							    __u64 vtime,
							    __u64 now)
{
	__u64 slice;
	__u64 flags;

	if (!package_root_copy_wakeup_preempt_candidate(p, ctx, policy, now))
		return false;

	slice = shared_wakeup_slice_ns();
	flags = enq_flags | SCX_ENQ_PREEMPT;
	remember_task_slice(p, slice);
	scx_bpf_kick_cpu(scx_bpf_task_cpu(p), SCX_KICK_PREEMPT);
	if (fifo_sched)
		scx_bpf_dsq_insert(p, FLOWCAUSAL_LATENCY_DSQ, slice, flags);
	else
		scx_bpf_dsq_insert_vtime(p, FLOWCAUSAL_LATENCY_DSQ, slice,
					 vtime, flags);
	bump_global_stat(FLOW_GSTAT_LATENCY_DSQ_ENQUEUES, 1);
	bump_global_stat(FLOW_GSTAT_SHARED_WAKEUP_PREEMPT_ENTRIES, 1);
	bump_mode_stat(FLOWCAUSAL_TASK_MODE_INTERACTIVE, true, false);
	return true;
}

static __always_inline bool consume_package_root_copy_latency(struct task_struct *p,
							     struct flow_task_ctx *ctx,
							     struct flow_session_policy *policy,
							     __u64 enq_flags,
							     __u64 vtime)
{
	__u64 slice;
	__u64 flags;

	if (!FLOWCAUSAL_DEFAULT_PACKAGE_ROOT_COPY_LATENCY)
		return false;
	if (!p || !package_root_copy_phase(ctx, policy))
		return false;

	slice = FLOWCAUSAL_DEFAULT_PACKAGE_ROOT_COPY_LATENCY_SLICE_NS ?
		FLOWCAUSAL_DEFAULT_PACKAGE_ROOT_COPY_LATENCY_SLICE_NS :
		shared_slice_ns();
	flags = enq_flags | SCX_ENQ_PREEMPT;
	remember_task_slice(p, slice);
	scx_bpf_kick_cpu(scx_bpf_task_cpu(p), SCX_KICK_PREEMPT);
	if (fifo_sched)
		scx_bpf_dsq_insert(p, FLOWCAUSAL_LATENCY_DSQ, slice, flags);
	else
		scx_bpf_dsq_insert_vtime(p, FLOWCAUSAL_LATENCY_DSQ, slice,
					 vtime, flags);
	bump_global_stat(FLOW_GSTAT_LATENCY_DSQ_ENQUEUES, 1);
	bump_mode_stat(FLOWCAUSAL_TASK_MODE_INTERACTIVE, true, false);
	return true;
}

static __always_inline __u32 effective_policy_anchor_span(struct flow_session_policy *policy,
							  struct flow_task_ctx *ctx)
{
	__u32 span;

	if (!policy || policy->anchor_cpu < 0)
		return 0;
	if (FLOWCAUSAL_DEFAULT_PACKAGE_RESCUE_CXX_CPUSET &&
	    policy->anchor_cpu == 5) {
		__u64 now = bpf_ktime_get_ns();

		if (package_avoid_py_test_active_now(now)) {
			if (FLOWCAUSAL_DEFAULT_PACKAGE_AVOID_PY_TEST_SPAN)
				return FLOWCAUSAL_DEFAULT_PACKAGE_AVOID_PY_TEST_SPAN;
			return 3;
		}
		if (FLOWCAUSAL_DEFAULT_PACKAGE_FINAL_TAIL_LOCALITY &&
		    package_final_tail_active_now(now)) {
			if (FLOWCAUSAL_DEFAULT_PACKAGE_FINAL_TAIL_SPAN)
				return FLOWCAUSAL_DEFAULT_PACKAGE_FINAL_TAIL_SPAN;
			return 8;
		}
		if ((package_rescue_active_now(now) &&
		     !py_test_protect_active_now(now) &&
		     (FLOWCAUSAL_DEFAULT_PACKAGE_RESCUE_ALL_LOCALITY ||
		      (ctx && (ctx->flags & FLOWCAUSAL_TASK_STAGE_BUILD_WIDE)))) ||
		    package_tail_locality_active_now(now)) {
		if (FLOWCAUSAL_DEFAULT_PACKAGE_RESCUE_SPAN)
			return FLOWCAUSAL_DEFAULT_PACKAGE_RESCUE_SPAN;
		return FLOWCAUSAL_DEFAULT_CXX_BUILD_SPAN ?
		       FLOWCAUSAL_DEFAULT_CXX_BUILD_SPAN : 3;
		}
	}
	if (ctx && (ctx->flags & FLOWCAUSAL_TASK_STAGE_BUILD_WIDE) &&
	    policy->anchor_cpu == 0)
		return FLOWCAUSAL_DEFAULT_CXX_BUILD_SPAN ?
		       FLOWCAUSAL_DEFAULT_CXX_BUILD_SPAN : 1;
	if (ctx && (ctx->flags & FLOWCAUSAL_TASK_STAGE_TEST_TAIL) &&
	    policy->anchor_cpu == 4) {
		if (py_test_burst_active_now(ctx, policy, bpf_ktime_get_ns())) {
			if (FLOWCAUSAL_DEFAULT_PY_TEST_BURST_SPAN)
				return FLOWCAUSAL_DEFAULT_PY_TEST_BURST_SPAN;
			return 8;
		}
		if (FLOWCAUSAL_DEFAULT_ROOT_PREP_PARTITION &&
		    (ctx->flags & FLOWCAUSAL_TASK_ROOT))
			return 2;
		if (FLOWCAUSAL_DEFAULT_PY_TEST_SPAN)
			return FLOWCAUSAL_DEFAULT_PY_TEST_SPAN;
		return 5;
	}
	if (ctx && policy->anchor_cpu == 5 &&
	    (ctx->flags & FLOWCAUSAL_TASK_ROOT) &&
	    !(ctx->flags & FLOWCAUSAL_TASK_ROOT_STAGE_STARTED) &&
	    FLOWCAUSAL_DEFAULT_PACKAGE_ROOT_COPY_SPAN)
		return FLOWCAUSAL_DEFAULT_PACKAGE_ROOT_COPY_SPAN;
	if (policy->anchor_cpu == 5 && FLOWCAUSAL_DEFAULT_PACKAGE_BASE_SPAN)
		return FLOWCAUSAL_DEFAULT_PACKAGE_BASE_SPAN;
	span = policy->anchor_span;
	if (!span)
		span = 1;
	if (ctx && (ctx->flags & FLOWCAUSAL_TASK_STAGE_BUILD_WIDE) &&
	    default_converge_wide_width > span)
		span = default_converge_wide_width;
	if (span > 8)
		span = 8;
	return span;
}

static __always_inline __s32 effective_policy_anchor_cpu(struct flow_session_policy *policy,
							 struct flow_task_ctx *ctx)
{
	if (!policy || policy->anchor_cpu < 0)
		return -1;
	if (FLOWCAUSAL_DEFAULT_PACKAGE_RESCUE_CXX_CPUSET &&
	    policy->anchor_cpu == 5) {
		__u64 now = bpf_ktime_get_ns();

		if (package_avoid_py_test_active_now(now))
			return FLOWCAUSAL_DEFAULT_PACKAGE_AVOID_PY_TEST_ANCHOR_CPU;
		if (FLOWCAUSAL_DEFAULT_PACKAGE_FINAL_TAIL_LOCALITY &&
		    package_final_tail_active_now(now))
			return FLOWCAUSAL_DEFAULT_PACKAGE_FINAL_TAIL_ANCHOR_CPU;
		if ((package_rescue_active_now(now) &&
		     !py_test_protect_active_now(now) &&
		     (FLOWCAUSAL_DEFAULT_PACKAGE_RESCUE_ALL_LOCALITY ||
		      (ctx && (ctx->flags & FLOWCAUSAL_TASK_STAGE_BUILD_WIDE)))) ||
		    package_tail_locality_active_now(now)) {
		if (FLOWCAUSAL_DEFAULT_PACKAGE_RESCUE_ANCHOR_CPU >= 0)
			return FLOWCAUSAL_DEFAULT_PACKAGE_RESCUE_ANCHOR_CPU;
		return 0;
		}
	}
	if (ctx && (ctx->flags & FLOWCAUSAL_TASK_STAGE_TEST_TAIL) &&
	    policy->anchor_cpu == 4) {
		if (py_test_burst_active_now(ctx, policy, bpf_ktime_get_ns()))
			return FLOWCAUSAL_DEFAULT_PY_TEST_BURST_ANCHOR_CPU;
		return FLOWCAUSAL_DEFAULT_PY_TEST_ANCHOR_CPU >= 0 ?
		       FLOWCAUSAL_DEFAULT_PY_TEST_ANCHOR_CPU : 3;
	}
	if (ctx && policy->anchor_cpu == 5 &&
	    (ctx->flags & FLOWCAUSAL_TASK_ROOT) &&
	    !(ctx->flags & FLOWCAUSAL_TASK_ROOT_STAGE_STARTED) &&
	    FLOWCAUSAL_DEFAULT_PACKAGE_ROOT_COPY_ANCHOR_CPU >= 0)
		return FLOWCAUSAL_DEFAULT_PACKAGE_ROOT_COPY_ANCHOR_CPU;
	if (policy->anchor_cpu == 5 &&
	    FLOWCAUSAL_DEFAULT_PACKAGE_BASE_ANCHOR_CPU >= 0)
		return FLOWCAUSAL_DEFAULT_PACKAGE_BASE_ANCHOR_CPU;
	if (ctx && (ctx->flags & FLOWCAUSAL_TASK_STAGE_BUILD_WIDE) &&
	    policy->anchor_cpu == 0 &&
	    FLOWCAUSAL_DEFAULT_CXX_BUILD_ANCHOR_CPU >= 0)
		return FLOWCAUSAL_DEFAULT_CXX_BUILD_ANCHOR_CPU;
	return policy->anchor_cpu;
}

static __always_inline bool task_matches_compile_chain(struct task_struct *p,
						       struct flow_task_ctx *ctx,
						       struct flow_session_policy *policy)
{
	char comm[FLOWCAUSAL_MAX_COMM] = {};

	if (!p || !ctx || !policy)
		return false;
	if (!(ctx->flags & FLOWCAUSAL_TASK_STAGE_BUILD_WIDE))
		return false;
	if (policy->anchor_cpu != 0)
		return false;
	if (bpf_probe_read_kernel_str(comm, sizeof(comm), p->comm) <= 0)
		return false;
	return comm_is_compile_chain(comm);
}

static __always_inline bool cxx_first_window_active(__u64 now)
{
	if (!default_cxx_first_window_ns || !scheduler_start_ns)
		return false;
	return now - scheduler_start_ns < default_cxx_first_window_ns;
}

static __always_inline bool cxx_first_suppresses_task(struct flow_task_ctx *ctx,
						      struct flow_session_policy *policy,
						      __u64 now)
{
	if (FLOWCAUSAL_DEFAULT_CXX_FIRST_SUPPRESS_UNTIL_CXX_STAGE_STARTED) {
		if (!ctx || !policy)
			return false;
		if (cxx_root_stage_started)
			return false;
		if (!(ctx->flags & FLOWCAUSAL_TASK_FRONTIER))
			return false;
		return policy->anchor_cpu != 0 &&
		       (ctx->flags & FLOWCAUSAL_TASK_ROOT) &&
		       !(ctx->flags & FLOWCAUSAL_TASK_ROOT_STAGE_STARTED);
	}
	if (!cxx_first_window_active(now) || !ctx || !policy)
		return false;
	if (!(ctx->flags & FLOWCAUSAL_TASK_FRONTIER))
		return false;
	if (FLOWCAUSAL_DEFAULT_CXX_FIRST_SUPPRESS_NONCXX_ROOT_COPY)
		return policy->anchor_cpu != 0 &&
		       (ctx->flags & FLOWCAUSAL_TASK_ROOT) &&
		       !(ctx->flags & FLOWCAUSAL_TASK_ROOT_STAGE_STARTED);
	if (FLOWCAUSAL_DEFAULT_FIRST_SUPPRESS_NONPACKAGE_ROOT_COPY)
		return policy->anchor_cpu != 5 &&
		       (ctx->flags & FLOWCAUSAL_TASK_ROOT) &&
		       !(ctx->flags & FLOWCAUSAL_TASK_ROOT_STAGE_STARTED);
	if (FLOWCAUSAL_DEFAULT_CXX_FIRST_SUPPRESS_PACKAGE_ONLY &&
	    policy->anchor_cpu != 5)
		return false;
	if (FLOWCAUSAL_DEFAULT_CXX_FIRST_SUPPRESS_ROOT_COPY_ONLY)
		return (ctx->flags & FLOWCAUSAL_TASK_ROOT) &&
		       !(ctx->flags & FLOWCAUSAL_TASK_ROOT_STAGE_STARTED);
	if (FLOWCAUSAL_DEFAULT_CXX_FIRST_SUPPRESS_NONROOT_ONLY &&
	    (ctx->flags & FLOWCAUSAL_TASK_ROOT))
		return false;
	if (FLOWCAUSAL_DEFAULT_CXX_FIRST_SUPPRESS_PACKAGE_ONLY)
		return ctx->flags & FLOWCAUSAL_TASK_STAGE_BUILD_WIDE;
	if (policy->anchor_cpu == 0)
		return false;
	return true;
}

static __always_inline s32 cxx_first_suppress_cpu(void)
{
	return (s32)FLOWCAUSAL_DEFAULT_CXX_FIRST_SUPPRESS_CPU;
}

static __always_inline bool cpu_in_policy_anchor_span(struct task_struct *p,
						      struct flow_session_policy *policy,
						      struct flow_task_ctx *ctx,
						      s32 cpu)
{
	__u32 span;
	__s32 anchor_cpu;
	__s32 logical_cpu;

	if (!policy || policy->anchor_cpu < 0 || cpu < 0)
		return false;
	anchor_cpu = effective_policy_anchor_cpu(policy, ctx);
	if (anchor_cpu < 0)
		return false;
	span = effective_policy_anchor_span(policy, ctx);
	if (!span)
		return false;
	logical_cpu = FLOWCAUSAL_DEFAULT_CPUSET_LANE_MAP ?
		      flowcausal_allowed_cpu_ordinal(p, cpu) : cpu;
	if (logical_cpu < anchor_cpu)
		return false;
	return (__u32)(logical_cpu - anchor_cpu) < span;
}

static __always_inline bool task_is_direct_child_of_root_anchor(struct flow_task_ctx *ctx,
							       s32 anchor_cpu)
{
	struct flow_task_key parent_key = {};
	struct flow_task_ctx *parent;

	if (!ctx || !ctx->parent_pid)
		return false;
	parent_key.pid = ctx->parent_pid;
	parent = bpf_map_lookup_elem(&task_ctx, &parent_key);
	if (!parent)
		return false;
	if (!(parent->flags & FLOWCAUSAL_TASK_ROOT))
		return false;
	return parent->anchor_cpu == anchor_cpu;
}

static __always_inline __u32 next_root_direct_child_seq(struct flow_task_key *root_key)
{
	__u32 one = 1;
	__u32 next;
	__u32 *seq;

	if (!root_key)
		return 0;
	seq = bpf_map_lookup_elem(&root_direct_child_seq, root_key);
	if (!seq) {
		bpf_map_update_elem(&root_direct_child_seq, root_key, &one,
				    BPF_ANY);
		return one;
	}
	next = *seq + 1;
	bpf_map_update_elem(&root_direct_child_seq, root_key, &next, BPF_ANY);
	return next;
}

static __always_inline void
classify_root_direct_child_by_seq(struct flow_task_ctx *child_ctx,
				  struct flow_task_ctx *parent,
				  struct flow_task_key *parent_key)
{
	__u32 seq;
	__u32 keep;

	if (!FLOWCAUSAL_DEFAULT_ROOT_DIRECT_SEQ_STAGE)
		return;
	if (!child_ctx || !parent || !parent_key)
		return;
	if (!(parent->flags & FLOWCAUSAL_TASK_ROOT))
		return;

	seq = next_root_direct_child_seq(parent_key);
	keep = child_ctx->flags &
	       (FLOWCAUSAL_TASK_LOCALITY |
		FLOWCAUSAL_TASK_C_ASSIST |
		FLOWCAUSAL_TASK_BPF_AUTO);
	child_ctx->flags = keep;
	child_ctx->propagation_depth = parent->propagation_depth > 0 ?
				       parent->propagation_depth - 1 : 0;
	if (child_ctx->propagation_depth)
		child_ctx->flags |= FLOWCAUSAL_TASK_PROPAGATE;

	if (seq == 1) {
		child_ctx->flags |= FLOWCAUSAL_TASK_STAGE_INSPECT |
				    FLOWCAUSAL_TASK_FRONTIER |
				    FLOWCAUSAL_TASK_COHORT_TAIL;
	} else if (seq == 2) {
		child_ctx->flags |= FLOWCAUSAL_TASK_STAGE_BUILD_WIDE |
				    FLOWCAUSAL_TASK_FRONTIER;
	} else if (seq == 3) {
		child_ctx->flags |= FLOWCAUSAL_TASK_STAGE_TEST_TAIL |
				    FLOWCAUSAL_TASK_FRONTIER |
				    FLOWCAUSAL_TASK_COHORT_TAIL |
				    FLOWCAUSAL_TASK_CONVERGE_TAIL;
		if (FLOWCAUSAL_DEFAULT_PACKAGE_RESCUE_WAIT_PY_TEST &&
		    parent->anchor_cpu == 4)
			py_test_protect_until_ns =
				bpf_ktime_get_ns() +
				FLOWCAUSAL_DEFAULT_PY_TEST_PROTECT_MAX_NS;
	} else {
		child_ctx->flags |= FLOWCAUSAL_TASK_STAGE_PACKAGE_BG |
				    FLOWCAUSAL_TASK_BACKGROUND;
		child_ctx->flags &= ~(FLOWCAUSAL_TASK_FRONTIER |
				      FLOWCAUSAL_TASK_COHORT_TAIL |
				      FLOWCAUSAL_TASK_CONVERGE_TAIL);
	}
}

static __always_inline void
mark_package_bg_direct_child_by_seq(struct flow_task_ctx *child_ctx,
				    struct flow_task_ctx *parent,
				    struct flow_task_key *parent_key)
{
	__u32 seq;

	if (!FLOWCAUSAL_DEFAULT_PACKAGE_BG_SEQ_MARK)
		return;
	if (FLOWCAUSAL_DEFAULT_ROOT_DIRECT_SEQ_STAGE)
		return;
	if (!child_ctx || !parent || !parent_key)
		return;
	if (!(parent->flags & FLOWCAUSAL_TASK_ROOT))
		return;
	if (parent->anchor_cpu != 5)
		return;

	seq = next_root_direct_child_seq(parent_key);
	if (seq >= 4)
		child_ctx->flags |= FLOWCAUSAL_TASK_PACKAGE_BG_MARK;
}

static __always_inline void
classify_package_direct_child_by_seq(struct flow_task_ctx *child_ctx,
				     struct flow_task_ctx *parent,
				     struct flow_task_key *parent_key)
{
	__u32 seq;
	__u32 keep;

	if (!FLOWCAUSAL_DEFAULT_PACKAGE_DIRECT_SEQ_STAGE)
		return;
	if (FLOWCAUSAL_DEFAULT_ROOT_DIRECT_SEQ_STAGE ||
	    FLOWCAUSAL_DEFAULT_PACKAGE_BG_SEQ_MARK)
		return;
	if (!child_ctx || !parent || !parent_key)
		return;
	if (!(parent->flags & FLOWCAUSAL_TASK_ROOT))
		return;
	if (parent->anchor_cpu != 5)
		return;

	seq = next_root_direct_child_seq(parent_key);
	keep = child_ctx->flags &
	       (FLOWCAUSAL_TASK_LOCALITY |
		FLOWCAUSAL_TASK_C_ASSIST |
		FLOWCAUSAL_TASK_BPF_AUTO);
	child_ctx->flags = keep;
	child_ctx->propagation_depth = parent->propagation_depth > 0 ?
				       parent->propagation_depth - 1 : 0;
	if (child_ctx->propagation_depth)
		child_ctx->flags |= FLOWCAUSAL_TASK_PROPAGATE;

	if (seq == 1) {
		child_ctx->flags |= FLOWCAUSAL_TASK_STAGE_INSPECT;
	} else if (seq == 2) {
		child_ctx->flags |= FLOWCAUSAL_TASK_STAGE_BUILD_WIDE |
				    FLOWCAUSAL_TASK_FRONTIER;
	} else if (seq == 3) {
		child_ctx->flags |= FLOWCAUSAL_TASK_STAGE_TEST_TAIL |
				    FLOWCAUSAL_TASK_FRONTIER |
				    FLOWCAUSAL_TASK_COHORT_TAIL;
	} else {
		child_ctx->flags |= FLOWCAUSAL_TASK_STAGE_PACKAGE_BG |
				    FLOWCAUSAL_TASK_BACKGROUND |
				    FLOWCAUSAL_TASK_PACKAGE_BG_MARK;
		child_ctx->flags &= ~(FLOWCAUSAL_TASK_FRONTIER |
				      FLOWCAUSAL_TASK_COHORT_TAIL |
				      FLOWCAUSAL_TASK_CONVERGE_TAIL);
	}
}

static __always_inline void
classify_package_test_direct_child_by_seq(struct flow_task_ctx *child_ctx,
					  struct flow_task_ctx *parent,
					  struct flow_task_key *parent_key)
{
	__u32 seq;
	__u32 keep;

	if (!FLOWCAUSAL_DEFAULT_PACKAGE_TEST_SEQ_STAGE_ONLY)
		return;
	if (FLOWCAUSAL_DEFAULT_ROOT_DIRECT_SEQ_STAGE ||
	    FLOWCAUSAL_DEFAULT_PACKAGE_BG_SEQ_MARK ||
	    FLOWCAUSAL_DEFAULT_PACKAGE_DIRECT_SEQ_STAGE)
		return;
	if (!child_ctx || !parent || !parent_key)
		return;
	if (!(parent->flags & FLOWCAUSAL_TASK_ROOT))
		return;
	if (parent->anchor_cpu != 5)
		return;

	seq = next_root_direct_child_seq(parent_key);
	if (seq != 3)
		return;

	keep = child_ctx->flags &
	       (FLOWCAUSAL_TASK_LOCALITY |
		FLOWCAUSAL_TASK_C_ASSIST |
		FLOWCAUSAL_TASK_BPF_AUTO);
	child_ctx->flags = keep |
			   FLOWCAUSAL_TASK_STAGE_TEST_TAIL |
			   FLOWCAUSAL_TASK_FRONTIER |
			   FLOWCAUSAL_TASK_COHORT_TAIL;
	child_ctx->propagation_depth = parent->propagation_depth > 0 ?
				       parent->propagation_depth - 1 : 0;
	if (child_ctx->propagation_depth)
		child_ctx->flags |= FLOWCAUSAL_TASK_PROPAGATE;
}

static __always_inline void
mark_cxx_third_direct_child_by_seq(struct flow_task_ctx *child_ctx,
				   struct flow_task_ctx *parent,
				   struct flow_task_key *parent_key)
{
	__u32 seq;

	if (!FLOWCAUSAL_DEFAULT_PACKAGE_RESCUE_AFTER_CXX_THIRD_CHILD)
		return;
	if (FLOWCAUSAL_DEFAULT_ROOT_DIRECT_SEQ_STAGE ||
	    FLOWCAUSAL_DEFAULT_PACKAGE_BG_SEQ_MARK)
		return;
	if (!child_ctx || !parent || !parent_key)
		return;
	if (!(parent->flags & FLOWCAUSAL_TASK_ROOT))
		return;
	if (parent->anchor_cpu != 0)
		return;

	seq = next_root_direct_child_seq(parent_key);
	if (seq == 3)
		child_ctx->flags |= FLOWCAUSAL_TASK_ROOT_DIRECT_THIRD;
}

static __always_inline void
mark_critical_direct_child_by_seq(struct flow_task_ctx *child_ctx,
				  struct flow_task_ctx *parent,
				  struct flow_task_key *parent_key)
{
	__u32 seq;

	if (!FLOWCAUSAL_DEFAULT_PACKAGE_FINAL_TAIL_AFTER_CRITICAL_CHILDREN &&
	    !FLOWCAUSAL_DEFAULT_PARENT_EXIT_CLEANUP_PACKAGE_AFTER_CRITICAL &&
	    !FLOWCAUSAL_DEFAULT_PARENT_EXIT_CLEANUP_SKIP_WHILE_PY_CRITICAL_ACTIVE &&
	    !FLOWCAUSAL_DEFAULT_PY_TEST_RUNTIME_TAIL_AFTER_PACKAGE_CRITICAL)
		return;
	if (FLOWCAUSAL_DEFAULT_ROOT_DIRECT_SEQ_STAGE ||
	    FLOWCAUSAL_DEFAULT_PACKAGE_BG_SEQ_MARK ||
	    FLOWCAUSAL_DEFAULT_PACKAGE_TEST_SEQ_STAGE_ONLY ||
	    FLOWCAUSAL_DEFAULT_PACKAGE_RESCUE_AFTER_CXX_THIRD_CHILD)
		return;
	if (!child_ctx || !parent || !parent_key)
		return;
	if (!(parent->flags & FLOWCAUSAL_TASK_ROOT))
		return;

	seq = next_root_direct_child_seq(parent_key);
	if (parent->anchor_cpu == 0 && seq == 2)
		child_ctx->flags |= FLOWCAUSAL_TASK_ROOT_DIRECT_SECOND;
	else if ((parent->anchor_cpu == 4 || parent->anchor_cpu == 5) &&
		 seq == 3) {
		child_ctx->flags |= FLOWCAUSAL_TASK_ROOT_DIRECT_THIRD;
		if (parent->anchor_cpu == 4)
			py_critical_child_started = true;
	}
}

static __always_inline s32 policy_anchor_span_cpu(struct task_struct *p,
						  struct flow_session_policy *policy,
						  struct flow_task_ctx *ctx,
						  s32 prev_cpu)
{
	__u32 span;
	__u32 off;
	__u32 i;
	s32 remembered;
	s32 anchor_cpu;
	s32 cpu;

	if (!p || !policy || policy->anchor_cpu < 0)
		return prev_cpu;

	anchor_cpu = effective_policy_anchor_cpu(policy, ctx);
	if (anchor_cpu < 0)
		return prev_cpu;
	span = effective_policy_anchor_span(policy, ctx);
	if (!span)
		return prev_cpu;

	if (cxx_first_suppresses_task(ctx, policy, bpf_ktime_get_ns())) {
		cpu = flowcausal_lane_cpu(p, cxx_first_suppress_cpu());

		if (task_can_run_on_sched_cpu(p, cpu))
			return cpu;
	}

	if (default_compile_chain_local &&
	    task_matches_compile_chain(p, ctx, policy) &&
	    task_can_run_on_sched_cpu(p, flowcausal_lane_cpu(p, anchor_cpu)))
		return flowcausal_lane_cpu(p, anchor_cpu);

	if (FLOWCAUSAL_DEFAULT_PY_TEST_IDLE_PICK &&
	    ctx &&
	    policy->anchor_cpu == 4 &&
	    (ctx->flags & FLOWCAUSAL_TASK_STAGE_TEST_TAIL)) {
		off = (__u32)p->pid % span;
#pragma unroll
		for (i = 0; i < 8; i++) {
			__u32 idx;

			if (i >= span)
				break;
			idx = off + i;
			if (idx >= span)
				idx -= span;
			cpu = flowcausal_span_candidate_cpu(p, anchor_cpu, idx);
			if (task_can_run_on_sched_cpu(p, cpu) &&
			    scx_bpf_test_and_clear_cpu_idle(cpu))
				return cpu;
		}
	}

	if (FLOWCAUSAL_DEFAULT_ANCHOR_SPAN_IDLE_PICK) {
		off = (__u32)p->pid % span;
#pragma unroll
		for (i = 0; i < 8; i++) {
			__u32 idx;

			if (i >= span)
				break;
			idx = off + i;
			if (idx >= span)
				idx -= span;
			cpu = flowcausal_span_candidate_cpu(p, anchor_cpu, idx);
			if (task_can_run_on_sched_cpu(p, cpu) &&
			    scx_bpf_test_and_clear_cpu_idle(cpu))
				return cpu;
		}
	}

	if (cpu_in_policy_anchor_span(p, policy, ctx, prev_cpu) &&
	    task_can_run_on_sched_cpu(p, prev_cpu))
		return prev_cpu;

	remembered = remembered_task_cpu(p);
	if (cpu_in_policy_anchor_span(p, policy, ctx, remembered) &&
	    task_can_run_on_sched_cpu(p, remembered))
		return remembered;

	off = (__u32)p->pid % span;
#pragma unroll
	for (i = 0; i < 8; i++) {
		__u32 idx;

		if (i >= span)
			break;
		idx = off + i;
		if (idx >= span)
			idx -= span;
		cpu = flowcausal_span_candidate_cpu(p, anchor_cpu, idx);
		if (task_can_run_on_sched_cpu(p, cpu))
			return cpu;
	}

	cpu = flowcausal_lane_cpu(p, anchor_cpu);
	if (task_can_run_on_sched_cpu(p, cpu))
		return cpu;
	return prev_cpu;
}

static __always_inline bool consume_root_child_wakeup(struct task_struct *p,
						      struct flow_task_key *key,
						      struct flow_task_ctx *ctx,
						      __u64 enq_flags)
{
	struct flow_session_policy policy;
	__u64 *until;
	__u64 now;
	__u64 slice;
	s32 cpu;

	if (!FLOWCAUSAL_DEFAULT_ROOT_CHILD_WAKEUP)
		return false;
	if (!p || !key || !ctx)
		return false;
	if (!(ctx->flags & FLOWCAUSAL_TASK_ROOT))
		return false;
	if (ctx->session_id == FLOWCAUSAL_PARENT_EXIT_SESSION_ID)
		return false;

	until = bpf_map_lookup_elem(&root_child_wakeup_until, key);
	if (!until)
		return false;
	now = bpf_ktime_get_ns();
	bpf_map_delete_elem(&root_child_wakeup_until, key);
	if (time_after(now, *until))
		return false;

	policy = lookup_policy_or_default_ctx(ctx);
	if (!(policy.flags & FLOWCAUSAL_POLICY_LOCALITY) ||
	    policy.anchor_cpu < 0)
		return false;

	cpu = policy_anchor_span_cpu(p, &policy, ctx, scx_bpf_task_cpu(p));
	if (!task_can_run_on_sched_cpu(p, cpu))
		return false;

	slice = shared_slice_ns();
	remember_task_cpu(p, cpu);
	remember_task_slice(p, slice);
	if (FLOWCAUSAL_DEFAULT_ROOT_CHILD_WAKEUP_PREEMPT) {
		scx_bpf_kick_cpu(cpu, SCX_KICK_PREEMPT);
		enq_flags |= SCX_ENQ_PREEMPT;
	}
	scx_bpf_dsq_insert(p, SCX_DSQ_LOCAL_ON | (__u64)cpu, slice,
			   enq_flags);
	bump_global_stat(FLOW_GSTAT_LATENCY_DSQ_ENQUEUES, 1);
	bump_mode_stat(FLOWCAUSAL_TASK_MODE_INTERACTIVE, true, false);
	return true;
}

static __always_inline void root_active_child_inc(struct flow_task_key *parent_key)
{
	__u32 one = 1;
	__u32 *cnt;

	if (!FLOWCAUSAL_DEFAULT_ROOT_CHILD_WAKEUP_ZERO_ONLY)
		return;
	if (!parent_key)
		return;

	cnt = bpf_map_lookup_elem(&root_active_children, parent_key);
	if (cnt) {
		__sync_fetch_and_add(cnt, 1);
		return;
	}
	bpf_map_update_elem(&root_active_children, parent_key, &one, BPF_ANY);
}

static __always_inline bool root_active_child_dec_to_zero(struct flow_task_ctx *task)
{
	struct flow_task_key parent_key = {};
	__u32 *cnt;

	if (!FLOWCAUSAL_DEFAULT_ROOT_CHILD_WAKEUP_ZERO_ONLY)
		return true;
	if (!task || !task->parent_pid)
		return true;

	parent_key.pid = task->parent_pid;
	cnt = bpf_map_lookup_elem(&root_active_children, &parent_key);
	if (!cnt)
		return true;
	if (*cnt <= 1) {
		bpf_map_delete_elem(&root_active_children, &parent_key);
		return true;
	}
	__sync_fetch_and_add(cnt, -1);
	return false;
}

static __always_inline bool critical_frontier_preempt_ctx(struct task_struct *p,
							  struct flow_task_ctx *ctx,
							  struct flow_session_policy *policy,
							  __u64 now)
{
	__u64 *last_ns;
	__u64 sid;

	if (!FLOWCAUSAL_DEFAULT_CRITICAL_FRONTIER_PREEMPT)
		return false;
	if (!ctx || !policy)
		return false;
	if (!(policy->anchor_cpu == 0 &&
	      (ctx->flags & FLOWCAUSAL_TASK_STAGE_BUILD_WIDE)) &&
	    !(policy->anchor_cpu == 4 &&
	      (ctx->flags & FLOWCAUSAL_TASK_STAGE_TEST_TAIL)))
		return false;
	if (FLOWCAUSAL_DEFAULT_CRITICAL_FRONTIER_BLOCKED_ONLY &&
	    !last_stop_was_blocked(p))
		return false;
	if (!FLOWCAUSAL_DEFAULT_CRITICAL_FRONTIER_COOLDOWN_NS)
		return true;

	sid = ctx->session_id;
	last_ns = bpf_map_lookup_elem(&critical_frontier_preempt_last, &sid);
	if (last_ns && time_after(now, *last_ns) &&
	    now - *last_ns < FLOWCAUSAL_DEFAULT_CRITICAL_FRONTIER_COOLDOWN_NS)
		return false;
	bpf_map_update_elem(&critical_frontier_preempt_last, &sid, &now, BPF_ANY);
	return true;
}

static __always_inline bool critical_wait_head_ctx(struct flow_task_ctx *ctx,
						   struct flow_session_policy *policy)
{
	if (!FLOWCAUSAL_DEFAULT_CRITICAL_WAIT_HEAD)
		return false;
	if (!ctx || !policy)
		return false;
	if (!(ctx->flags & FLOWCAUSAL_TASK_FRONTIER))
		return false;
	if (policy->anchor_cpu == 0 &&
	    (ctx->flags & FLOWCAUSAL_TASK_STAGE_BUILD_WIDE))
		goto stage_ok;
	if (!FLOWCAUSAL_DEFAULT_CRITICAL_WAIT_HEAD_CXX_ONLY &&
	    policy->anchor_cpu == 4 &&
	    (ctx->flags & FLOWCAUSAL_TASK_STAGE_TEST_TAIL))
		goto stage_ok;
	return false;
stage_ok:
	if (ctx->runnable_wait_ns < FLOWCAUSAL_DEFAULT_CRITICAL_WAIT_HEAD_NS)
		return false;
	return true;
}

static __always_inline bool py_test_runtime_tail_ctx(struct flow_task_ctx *ctx,
						     struct flow_session_policy *policy)
{
	if (!FLOWCAUSAL_DEFAULT_PY_TEST_RUNTIME_TAIL_LATENCY &&
	    !FLOWCAUSAL_DEFAULT_PY_TEST_RUNTIME_TAIL_LOCAL)
		return false;
	if (!ctx || !policy)
		return false;
	if (policy->anchor_cpu != 4)
		return false;
	if (!(ctx->flags & FLOWCAUSAL_TASK_STAGE_TEST_TAIL))
		return false;
	if (ctx->flags & FLOWCAUSAL_TASK_ROOT)
		return false;
	if (FLOWCAUSAL_DEFAULT_PY_TEST_RUNTIME_TAIL_RESCUE_ONLY &&
	    !package_rescue_active_now(bpf_ktime_get_ns()))
		return false;
	if (FLOWCAUSAL_DEFAULT_PY_TEST_RUNTIME_TAIL_AFTER_PACKAGE_CRITICAL &&
	    !package_critical_child_exited)
		return false;
	return ctx->runtime_ns >= FLOWCAUSAL_DEFAULT_PY_TEST_RUNTIME_TAIL_NS;
}

static __always_inline s32 py_test_runtime_tail_local_cpu(struct task_struct *p)
{
	__u32 span = FLOWCAUSAL_DEFAULT_PY_TEST_RUNTIME_TAIL_LOCAL_SPAN;
	s32 anchor_cpu = FLOWCAUSAL_DEFAULT_PY_TEST_RUNTIME_TAIL_LOCAL_ANCHOR_CPU;
	s32 remembered;
	s32 current;
	__u32 off;
	__u32 i;

	if (!p)
		return -1;
	if (!span)
		return -1;
	if (span > 8)
		span = 8;

	remembered = remembered_task_cpu(p);
	if (remembered >= anchor_cpu &&
	    (__u32)(remembered - anchor_cpu) < span &&
	    task_can_run_on_sched_cpu(p, remembered))
		return remembered;

	current = scx_bpf_task_cpu(p);
	if (current >= anchor_cpu &&
	    (__u32)(current - anchor_cpu) < span &&
	    task_can_run_on_sched_cpu(p, current))
		return current;

	off = (__u32)p->pid % span;
#pragma unroll
	for (i = 0; i < 8; i++) {
		__u32 idx;
		s32 cpu;

		if (i >= span)
			break;
		idx = off + i;
		if (idx >= span)
			idx -= span;
		cpu = anchor_cpu + (s32)idx;
		if (task_can_run_on_sched_cpu(p, cpu))
			return cpu;
	}

	return -1;
}

static __always_inline bool consume_py_test_runtime_tail(struct task_struct *p,
							 struct flow_task_ctx *ctx,
							 struct flow_session_policy *policy,
							 __u64 enq_flags,
							 __u64 vtime)
{
	__u64 slice;
	__u64 flags;
	s32 cpu;

	if (!py_test_runtime_tail_ctx(ctx, policy))
		return false;
	if (!p)
		return false;

	slice = FLOWCAUSAL_DEFAULT_PY_TEST_RUNTIME_TAIL_SLICE_NS ?
		FLOWCAUSAL_DEFAULT_PY_TEST_RUNTIME_TAIL_SLICE_NS : SCX_SLICE_DFL;
	flags = enq_flags;
	if (FLOWCAUSAL_DEFAULT_PY_TEST_RUNTIME_TAIL_LOCAL) {
		cpu = py_test_runtime_tail_local_cpu(p);
		if (task_can_run_on_sched_cpu(p, cpu)) {
			remember_task_cpu(p, cpu);
			remember_task_slice(p, slice);
			scx_bpf_kick_cpu(cpu, SCX_KICK_IDLE);
			scx_bpf_dsq_insert(p, SCX_DSQ_LOCAL_ON | (__u64)cpu,
					   slice, flags);
			bump_global_stat(FLOW_GSTAT_SHARED_LOCAL_ENQUEUES, 1);
			bump_global_stat(FLOW_GSTAT_SHARED_LOCAL_TRACKED_ENQUEUES, 1);
			bump_mode_stat(FLOWCAUSAL_TASK_MODE_TAIL, true, false);
			return true;
		}
	}
	if (FLOWCAUSAL_DEFAULT_PY_TEST_RUNTIME_TAIL_PREEMPT) {
		flags |= SCX_ENQ_PREEMPT;
		scx_bpf_kick_cpu(scx_bpf_task_cpu(p), SCX_KICK_PREEMPT);
	}

	remember_task_slice(p, slice);
	if (fifo_sched)
		scx_bpf_dsq_insert(p, FLOWCAUSAL_LATENCY_DSQ, slice, flags);
	else
		scx_bpf_dsq_insert_vtime(p, FLOWCAUSAL_LATENCY_DSQ, slice,
					 vtime, flags);
	bump_global_stat(FLOW_GSTAT_LATENCY_DSQ_ENQUEUES, 1);
	bump_mode_stat(FLOWCAUSAL_TASK_MODE_TAIL, true, false);
	return true;
}

static __always_inline bool enqueue_frontier_local(struct task_struct *p,
						   struct flow_task_ctx *ctx,
						   struct flow_session_policy *policy,
						   __u64 flags)
{
	__u64 slice = SCX_SLICE_DFL;
	s32 cpu;
	__u64 now;

	if (!default_frontier_local_dsq)
		return false;
	if (!ctx || !policy)
		return false;
	if (FLOWCAUSAL_DEFAULT_PACKAGE_RESCUE_FRONTIER_GLOBAL &&
	    policy->anchor_cpu == 5 &&
	    (ctx->flags & FLOWCAUSAL_TASK_STAGE_BUILD_WIDE) &&
	    package_rescue_active_now(bpf_ktime_get_ns()))
		return false;
	if (!(policy->flags & FLOWCAUSAL_POLICY_LOCALITY))
		return false;
	if (policy->anchor_cpu < 0)
		return false;

	cpu = policy_anchor_span_cpu(p, policy, ctx, scx_bpf_task_cpu(p));
	if (!task_can_run_on_sched_cpu(p, cpu))
		return false;

	now = bpf_ktime_get_ns();
	if (critical_frontier_preempt_ctx(p, ctx, policy, now)) {
		flags |= SCX_ENQ_PREEMPT;
		scx_bpf_kick_cpu(cpu, SCX_KICK_PREEMPT);
		bump_global_stat(FLOW_GSTAT_FRONTIER_WAKEUP_PREEMPT_ENTRIES, 1);
	}
	if (critical_wait_head_ctx(ctx, policy))
		flags |= SCX_ENQ_HEAD;
	if (FLOWCAUSAL_DEFAULT_COMPILE_CHAIN_SLICE &&
	    task_matches_compile_chain(p, ctx, policy)) {
		slice = FLOWCAUSAL_DEFAULT_COMPILE_CHAIN_SLICE_NS;
		if (!slice)
			slice = SCX_SLICE_DFL;
	}
	remember_task_cpu(p, cpu);
	remember_task_slice(p, slice);
	scx_bpf_dsq_insert(p, SCX_DSQ_LOCAL_ON | (__u64)cpu, slice, flags);
	return true;
}

static __always_inline bool critical_wait_frontier_stage(struct flow_task_ctx *ctx,
							 struct flow_session_policy *policy)
{
	if (!ctx || !policy)
		return false;
	if (!(ctx->flags & FLOWCAUSAL_TASK_FRONTIER))
		return false;
	if (ctx->flags & FLOWCAUSAL_TASK_ROOT)
		return false;
	if (policy->anchor_cpu == 0 &&
	    (ctx->flags & FLOWCAUSAL_TASK_STAGE_BUILD_WIDE))
		return true;
	if (policy->anchor_cpu == 4 &&
	    (ctx->flags & FLOWCAUSAL_TASK_STAGE_TEST_TAIL))
		return true;
	return false;
}

static __always_inline bool
consume_critical_wait_frontier_bypass(struct task_struct *p,
				      struct flow_task_ctx *ctx,
				      struct flow_session_policy *policy,
				      __u64 enq_flags,
				      __u32 event_flags)
{
	__u64 slice;
	s32 cpu;

	if (!FLOWCAUSAL_DEFAULT_CRITICAL_WAIT_FRONTIER_BYPASS)
		return false;
	if (!p || !critical_wait_frontier_stage(ctx, policy))
		return false;
	if (FLOWCAUSAL_DEFAULT_CRITICAL_WAIT_FRONTIER_BYPASS_REQUIRE_WIDTH_HELD &&
	    !(event_flags & (FLOWCAUSAL_TASK_WIDTH_HELD |
			     FLOWCAUSAL_TASK_PRESSURE_HELD)))
		return false;
	if (ctx->runnable_wait_ns <
	    FLOWCAUSAL_DEFAULT_CRITICAL_WAIT_FRONTIER_BYPASS_NS)
		return false;
	if (ctx->runtime_ns && ctx->runnable_wait_ns < (ctx->runtime_ns >> 3))
		return false;
	if (!(policy->flags & FLOWCAUSAL_POLICY_LOCALITY))
		return false;
	if (policy->anchor_cpu < 0)
		return false;

	cpu = policy_anchor_span_cpu(p, policy, ctx, scx_bpf_task_cpu(p));
	if (!task_can_run_on_sched_cpu(p, cpu))
		return false;

	slice = FLOWCAUSAL_DEFAULT_CRITICAL_WAIT_FRONTIER_BYPASS_SLICE_NS ?
		FLOWCAUSAL_DEFAULT_CRITICAL_WAIT_FRONTIER_BYPASS_SLICE_NS :
		SCX_SLICE_DFL;
	remember_task_cpu(p, cpu);
	remember_task_slice(p, slice);
	bump_session_stat(ctx->session_id, 1, 1);
	bump_global_stat(FLOW_GSTAT_FRONTIER_DSQ_ENQUEUES, 1);
	emit_event(FLOW_EVT_FRONTIER_ENQUEUE, p, ctx, ctx->flags);
	scx_bpf_dsq_insert(p, SCX_DSQ_LOCAL_ON | (__u64)cpu, slice, enq_flags);
	return true;
}

static __always_inline bool enqueue_root_copy_local(struct task_struct *p,
						    struct flow_task_ctx *ctx,
						    struct flow_session_policy *policy,
						    __u64 slice,
						    __u64 flags)
{
	s32 cpu;

	if (!root_copy_phase_local(ctx, policy))
		return false;
	if (!policy || !(policy->flags & FLOWCAUSAL_POLICY_LOCALITY))
		return false;
	if (policy->anchor_cpu < 0)
		return false;

	cpu = policy_anchor_span_cpu(p, policy, ctx, scx_bpf_task_cpu(p));
	if (!task_can_run_on_sched_cpu(p, cpu))
		return false;

	remember_task_cpu(p, cpu);
	remember_task_slice(p, slice);
	scx_bpf_dsq_insert(p, SCX_DSQ_LOCAL_ON | (__u64)cpu, slice, flags);
	bump_global_stat(FLOW_GSTAT_TRACKED_SHARED_PATH_ENQUEUES, 1);
	bump_global_stat(FLOW_GSTAT_SHARED_LOCAL_ENQUEUES, 1);
	bump_global_stat(FLOW_GSTAT_SHARED_LOCAL_TRACKED_ENQUEUES, 1);
	return true;
}

static __always_inline __u64 deadline_vtime(struct task_struct *p, __u64 vtime);

static __always_inline bool enqueue_root_copy_dsq(struct task_struct *p,
						  struct flow_task_ctx *ctx,
						  struct flow_session_policy *policy,
						  __u64 slice,
						  __u64 vtime,
						  __u64 flags)
{
	if (!FLOWCAUSAL_DEFAULT_ROOT_COPY_DSQ)
		return false;
	if (!p || !ctx || !policy)
		return false;
	if (!root_copy_phase(ctx))
		return false;

	remember_task_slice(p, slice);
	if (fifo_sched)
		scx_bpf_dsq_insert(p, FLOWCAUSAL_ROOT_COPY_DSQ, slice, flags);
	else
		scx_bpf_dsq_insert_vtime(p, FLOWCAUSAL_ROOT_COPY_DSQ, slice,
					 deadline_vtime(p, vtime), flags);
	bump_global_stat(FLOW_GSTAT_ROOT_COPY_DSQ_ENQUEUES, 1);
	return true;
}

static __always_inline bool enqueue_shared_local(struct task_struct *p,
						 __u64 slice,
						 __u64 flags,
						 bool tracked_task,
						 bool frontier_fallback_task)
{
	s32 cpu;

	if (!default_shared_local_dsq &&
	    !(default_tracked_local_dsq && tracked_task) &&
	    !(default_frontier_fallback_local_dsq && frontier_fallback_task))
		return false;

	cpu = scx_bpf_task_cpu(p);
	if (!task_can_run_on_sched_cpu(p, cpu)) {
		bump_global_stat(FLOW_GSTAT_SHARED_LOCAL_PICK_FALLBACKS, 1);
		cpu = scx_bpf_pick_any_cpu(p->cpus_ptr, 0);
	}
	if (!task_can_run_on_sched_cpu(p, cpu))
		return false;

	remember_task_slice(p, slice);
	scx_bpf_dsq_insert(p, SCX_DSQ_LOCAL_ON | (__u64)cpu, slice, flags);
	bump_global_stat(FLOW_GSTAT_SHARED_LOCAL_ENQUEUES, 1);
	if (tracked_task)
		bump_global_stat(FLOW_GSTAT_SHARED_LOCAL_TRACKED_ENQUEUES, 1);
	else
		bump_global_stat(FLOW_GSTAT_SHARED_LOCAL_UNTRACKED_ENQUEUES, 1);
	if (frontier_fallback_task)
		bump_global_stat(FLOW_GSTAT_SHARED_LOCAL_FRONTIER_FALLBACK_ENQUEUES, 1);
	return true;
}

static __always_inline __u64 shared_percpu_dsq_id(s32 cpu)
{
	return FLOWCAUSAL_CPU_SHARED_DSQ_BASE + (__u64)cpu;
}

static __always_inline __u32 session_frontier_slot(__u64 session_id)
{
	return (__u32)(session_id % FLOWCAUSAL_SESSION_FRONTIER_DSQ_SLOTS);
}

static __always_inline __u64 session_frontier_dsq_id(__u32 slot)
{
	return FLOWCAUSAL_SESSION_FRONTIER_DSQ_BASE + (__u64)slot;
}

static __always_inline bool task_is_converge_tail(struct flow_task_ctx *ctx,
						  __u32 mode, __u64 now)
{
	struct flow_session_runtime *s;
	__u64 runnable = 0;

	if (!default_converge || !ctx)
		return false;
	if (!(ctx->flags & FLOWCAUSAL_TASK_FRONTIER))
		return false;
	if (ctx->flags & (FLOWCAUSAL_TASK_STAGE_BUILD_WIDE |
			  FLOWCAUSAL_TASK_STAGE_PACKAGE_BG |
			  FLOWCAUSAL_TASK_STAGE_INSPECT))
		return false;
	if (ctx->flags & FLOWCAUSAL_TASK_STAGE_TEST_TAIL)
		return true;
	if (mode == FLOWCAUSAL_TASK_MODE_BACKGROUND ||
	    mode == FLOWCAUSAL_TASK_MODE_INTERACTIVE)
		return false;
	if (ctx->flags & FLOWCAUSAL_TASK_C_ASSIST)
		return false;

	s = bpf_map_lookup_elem(&session_runtime, &ctx->session_id);
	if (s)
		runnable = s->runnable_tasks;

	if (default_converge_wait_ns &&
	    ctx->runnable_wait_ns >= default_converge_wait_ns)
		return true;

	if (!runnable)
		return false;
	return runnable <= default_converge_tail_runnable;
}

static __always_inline bool critical_prefix_dsq_task(struct flow_task_ctx *ctx,
						     struct flow_session_policy *policy)
{
	if (!FLOWCAUSAL_DEFAULT_CRITICAL_PREFIX_DSQ)
		return false;
	if (!ctx || !policy)
		return false;
	if (!(ctx->flags & FLOWCAUSAL_TASK_FRONTIER))
		return false;
	if (ctx->flags & (FLOWCAUSAL_TASK_ROOT |
			  FLOWCAUSAL_TASK_BACKGROUND |
			  FLOWCAUSAL_TASK_STAGE_PACKAGE_BG |
			  FLOWCAUSAL_TASK_STAGE_INSPECT))
		return false;

	/*
	 * Critical-prefix work is the foreground prefix before the benchmark's
	 * critical marker: C++ BUILD and Python TEST.  Package BUILD can also
	 * be a prefix in synthetic traces, but earlier rescue experiments show
	 * that promoting package too early steals time from C++/Python, so keep
	 * it opt-in.
	 */
	if (policy->anchor_cpu == 0 &&
	    (ctx->flags & FLOWCAUSAL_TASK_STAGE_BUILD_WIDE))
		return true;
	if (policy->anchor_cpu == 4 &&
	    (ctx->flags & FLOWCAUSAL_TASK_STAGE_TEST_TAIL))
		return true;
	if (FLOWCAUSAL_DEFAULT_CRITICAL_PREFIX_INCLUDE_PACKAGE &&
	    policy->anchor_cpu == 5 &&
	    (ctx->flags & FLOWCAUSAL_TASK_STAGE_BUILD_WIDE))
		return true;
	return false;
}

static __always_inline __u32 effective_frontier_width(struct flow_task_ctx *ctx,
						      struct flow_session_policy *policy,
						      __u32 mode, __u64 now,
						      bool *tailp)
{
	struct flow_session_runtime *s;
	__u32 width = 0;
	__u64 runnable = 0;
	bool tail = false;

	if (policy)
		width = policy->max_frontier_width;
	if (!width)
		width = 1;
	if (tailp)
		*tailp = false;

	if (!default_adaptive_width || !ctx || !policy)
		return width;

	if (ctx->flags & FLOWCAUSAL_TASK_STAGE_BUILD_WIDE) {
		if (default_converge_wide_width > width)
			width = default_converge_wide_width;
		bump_global_stat(FLOW_GSTAT_ADAPTIVE_WIDTH_WIDE_ADMISSIONS, 1);
		return width;
	}
	if ((ctx->flags & FLOWCAUSAL_TASK_STAGE_TEST_TAIL) &&
	    policy->anchor_cpu == 4) {
		if (py_test_burst_active_now(ctx, policy, now)) {
			if (width < FLOWCAUSAL_DEFAULT_PY_TEST_BURST_WIDTH)
				width = FLOWCAUSAL_DEFAULT_PY_TEST_BURST_WIDTH;
			bump_global_stat(FLOW_GSTAT_ADAPTIVE_WIDTH_WIDE_ADMISSIONS, 1);
			return width;
		}
		if (width < 5)
			width = 5;
		bump_global_stat(FLOW_GSTAT_ADAPTIVE_WIDTH_WIDE_ADMISSIONS, 1);
		return width;
	}
	tail = task_is_converge_tail(ctx, mode, now);
	if (tail) {
		if (default_converge_tail_width)
			width = default_converge_tail_width;
		if (tailp)
			*tailp = true;
		bump_global_stat(FLOW_GSTAT_ADAPTIVE_WIDTH_TAIL_ADMISSIONS, 1);
		return width;
	}

	s = bpf_map_lookup_elem(&session_runtime, &ctx->session_id);
	if (s)
		runnable = s->runnable_tasks;

	if (mode != FLOWCAUSAL_TASK_MODE_INTERACTIVE &&
	    mode != FLOWCAUSAL_TASK_MODE_BACKGROUND &&
	    runnable > default_converge_tail_runnable &&
	    default_converge_wide_width > width) {
		width = default_converge_wide_width;
		bump_global_stat(FLOW_GSTAT_ADAPTIVE_WIDTH_WIDE_ADMISSIONS, 1);
	}
	return width;
}

static __always_inline bool valid_percpu_dsq_cpu(s32 cpu)
{
	if (!valid_sched_cpu(cpu))
		return false;
	if (cpu >= FLOWCAUSAL_MAX_CPUS)
		return false;
	return true;
}

static __always_inline bool task_can_use_percpu_dsq(struct task_struct *p,
						    s32 cpu)
{
	if (!valid_percpu_dsq_cpu(cpu))
		return false;
	/*
	 * A FlowCausal-owned per-CPU shared DSQ is serviced only by the matching
	 * dispatch CPU.  If a task is queued to a CPU outside its current
	 * affinity mask, sched_ext can report a runnable-task stall after the
	 * taskset/cpuset change because no allowed CPU will drain that DSQ for
	 * the task.  Treat the task's live cpus_ptr as authoritative at enqueue
	 * time and fall back to a safe shared path if no affinity-valid CPU can
	 * be found.
	 */
	if (!bpf_cpumask_test_cpu((__u32)cpu, p->cpus_ptr))
		return false;
	return true;
}

static __always_inline __u64 deadline_vtime(struct task_struct *p, __u64 vtime);

static __always_inline bool enqueue_shared_percpu(struct task_struct *p,
						  __u64 slice,
						  __u64 vtime,
						  __u64 flags,
						  bool tracked_task)
{
	s32 cpu;

	if (!default_shared_percpu_dsq)
		return false;

	cpu = remembered_task_cpu(p);
	if (!task_can_use_percpu_dsq(p, cpu)) {
		bump_global_stat(FLOW_GSTAT_SHARED_PERCPU_PICK_FALLBACKS, 1);
		cpu = scx_bpf_task_cpu(p);
	}
	if (!task_can_use_percpu_dsq(p, cpu)) {
		bump_global_stat(FLOW_GSTAT_SHARED_PERCPU_PICK_FALLBACKS, 1);
		cpu = scx_bpf_pick_any_cpu(p->cpus_ptr, 0);
	}
	if (!task_can_use_percpu_dsq(p, cpu))
		return false;

	remember_task_slice(p, slice);
	if (fifo_sched)
		scx_bpf_dsq_insert(p, shared_percpu_dsq_id(cpu), slice, flags);
	else
		scx_bpf_dsq_insert_vtime(p, shared_percpu_dsq_id(cpu),
					 slice, deadline_vtime(p, vtime), flags);
	bump_global_stat(FLOW_GSTAT_SHARED_PERCPU_ENQUEUES, 1);
	if (tracked_task)
		bump_global_stat(FLOW_GSTAT_SHARED_PERCPU_TRACKED_ENQUEUES, 1);
	else
		bump_global_stat(FLOW_GSTAT_SHARED_PERCPU_UNTRACKED_ENQUEUES, 1);
	return true;
}

static __always_inline bool enqueue_background(struct task_struct *p,
					       struct flow_task_ctx *tctx,
					       __u64 slice,
					       __u64 vtime,
					       __u64 flags)
{
	if (!default_background_dsq)
		return false;
	if (!tctx)
		return false;
	if (!(tctx->flags & FLOWCAUSAL_TASK_BACKGROUND))
		return false;
	if (tctx->flags & FLOWCAUSAL_TASK_FRONTIER)
		return false;

	remember_task_slice(p, slice);
	if (fifo_sched)
		scx_bpf_dsq_insert(p, FLOWCAUSAL_BACKGROUND_DSQ, slice, flags);
	else
		scx_bpf_dsq_insert_vtime(p, FLOWCAUSAL_BACKGROUND_DSQ,
					 slice, deadline_vtime(p, vtime), flags);
	bump_global_stat(27, 1);
	return true;
}

static __always_inline bool move_shared_to_local(s32 cpu)
{
	if (default_shared_percpu_dsq && valid_percpu_dsq_cpu(cpu)) {
		if (scx_bpf_dsq_move_to_local(shared_percpu_dsq_id(cpu))) {
			bump_global_stat(24, 1);
			if (default_mode_aware)
				bump_mode_stat(FLOWCAUSAL_TASK_MODE_COMPUTE, false, true);
			return true;
		}
		bump_global_stat(25, 1);
	}

	if (scx_bpf_dsq_move_to_local(FLOWCAUSAL_SHARED_DSQ)) {
		if (default_mode_aware)
			bump_mode_stat(FLOWCAUSAL_TASK_MODE_INTERACTIVE, false, true);
		return true;
	}
	return false;
}

static __always_inline bool move_session_frontier_to_local(void)
{
	__u32 start;
	__u32 i;

	if (!default_session_frontier_dsq)
		return false;

	start = session_frontier_rr_slot;
#pragma unroll
	for (i = 0; i < FLOWCAUSAL_SESSION_FRONTIER_DSQ_SLOTS; i++) {
		__u32 slot = start + i;

		if (slot >= FLOWCAUSAL_SESSION_FRONTIER_DSQ_SLOTS)
			slot -= FLOWCAUSAL_SESSION_FRONTIER_DSQ_SLOTS;
		if (scx_bpf_dsq_move_to_local(session_frontier_dsq_id(slot))) {
			slot++;
			if (slot >= FLOWCAUSAL_SESSION_FRONTIER_DSQ_SLOTS)
				slot = 0;
			session_frontier_rr_slot = slot;
			bump_global_stat(FLOW_GSTAT_SESSION_FRONTIER_DISPATCHES,
					 1);
			return true;
		}
	}
	return false;
}

static __always_inline void note_foreground_dispatch(void)
{
	__u32 root_copy_quota = FLOWCAUSAL_DEFAULT_ROOT_COPY_DSQ_DISPATCH_QUOTA;

	if (FLOWCAUSAL_DEFAULT_ROOT_COPY_DSQ &&
	    root_copy_quota &&
	    root_copy_dispatch_streak < 0xffffffffU)
		root_copy_dispatch_streak++;

	if (!default_background_dsq || !default_background_dispatch_quota)
		return;
	if (background_dispatch_streak < 0xffffffffU)
		background_dispatch_streak++;
}

static __always_inline bool maybe_dispatch_root_copy_quota(bool force)
{
	__u32 root_copy_quota = FLOWCAUSAL_DEFAULT_ROOT_COPY_DSQ_DISPATCH_QUOTA;

	if (!FLOWCAUSAL_DEFAULT_ROOT_COPY_DSQ)
		return false;
	if (!force &&
	    root_copy_quota &&
	    root_copy_dispatch_streak < root_copy_quota)
		return false;

	if (scx_bpf_dsq_move_to_local(FLOWCAUSAL_ROOT_COPY_DSQ)) {
		root_copy_dispatch_streak = 0;
		frontier_dispatch_streak = 0;
		bump_global_stat(FLOW_GSTAT_ROOT_COPY_DISPATCHES, 1);
		bump_mode_stat(FLOWCAUSAL_TASK_MODE_COMPUTE, false, true);
		return true;
	}
	return false;
}

static __always_inline bool maybe_dispatch_background_quota(void)
{
	if (!default_background_dsq || !default_background_dispatch_quota)
		return false;
	if (background_dispatch_streak < default_background_dispatch_quota)
		return false;

	background_dispatch_streak = 0;
	if (scx_bpf_dsq_move_to_local(FLOWCAUSAL_BACKGROUND_DSQ)) {
		frontier_dispatch_streak = 0;
		bump_global_stat(28, 1);
		return true;
	}
	return false;
}

static __always_inline __u64 accounted_slice_used(struct task_struct *p)
{
	struct flow_sched_task_state *state;
	__u64 charged_slice = SCX_SLICE_DFL;

	state = bpf_task_storage_get(&sched_task_state, p, 0, 0);
	if (state && state->last_slice_ns)
		charged_slice = state->last_slice_ns;

	if (p->scx.slice < charged_slice)
		return charged_slice - p->scx.slice;
	return charged_slice;
}

static __always_inline void refresh_frontier_dispatch_window(__u64 now)
{
	if (!frontier_dispatch_budget_enabled())
		return;

	if (!frontier_dispatch_window_start_ns ||
	    now - frontier_dispatch_window_start_ns >=
		    default_frontier_dispatch_window_ns) {
		frontier_dispatch_window_start_ns = now;
		frontier_dispatch_window_used = 0;
		bump_global_stat(10, 1);
	}
}

static __always_inline bool frontier_dispatch_budget_depleted(__u64 now)
{
	if (!frontier_dispatch_budget_enabled())
		return false;
	refresh_frontier_dispatch_window(now);
	return frontier_dispatch_window_used >=
	       default_frontier_dispatch_window_budget;
}

static __always_inline void consume_frontier_dispatch_budget(__u64 now)
{
	if (!frontier_dispatch_budget_enabled())
		return;
	refresh_frontier_dispatch_window(now);
	if (frontier_dispatch_window_used < 0xffffffffU)
		frontier_dispatch_window_used++;
}

static __always_inline bool pressure_fallback_enabled(void)
{
	return default_pressure_fallback_enqueues || default_pressure_fallback_ns;
}

static __always_inline bool pressure_dispatch_enabled(void)
{
	return default_pressure_dispatch_ns;
}

static __always_inline bool pressure_cpu_matches(__s32 cpu)
{
	if (!default_pressure_local_cpu)
		return true;
	if (pressure_cpu < 0)
		return true;
	return pressure_cpu == cpu;
}

static __always_inline void open_pressure_epoch(__u64 now, __s32 cpu)
{
	if (default_pressure_local_cpu)
		pressure_cpu = cpu;
	else
		pressure_cpu = -1;

	if (pressure_fallback_enabled()) {
		if (default_pressure_fallback_enqueues)
			pressure_fallback_remaining = default_pressure_fallback_enqueues;
		if (default_pressure_fallback_ns)
			pressure_fallback_until_ns = now + default_pressure_fallback_ns;
		bump_global_stat(8, 1);
	}

	if (pressure_dispatch_enabled()) {
		pressure_dispatch_until_ns = now + default_pressure_dispatch_ns;
		bump_global_stat(16, 1);
	}
}

static __always_inline bool pressure_dispatch_active(__u64 now, __s32 cpu)
{
	if (!pressure_dispatch_enabled())
		return false;
	if (!pressure_dispatch_until_ns)
		return false;
	if (now < pressure_dispatch_until_ns)
		return pressure_cpu_matches(cpu);
	pressure_dispatch_until_ns = 0;
	return false;
}

static __always_inline bool consume_pressure_fallback_slot(__u64 now, __s32 cpu)
{
	bool active = false;

	if (!pressure_fallback_enabled())
		return false;

	if (default_pressure_fallback_ns && pressure_fallback_until_ns) {
		if (now < pressure_fallback_until_ns)
			active = true;
		else
			pressure_fallback_until_ns = 0;
	}

	if (!pressure_cpu_matches(cpu))
		return false;

	if (pressure_fallback_remaining) {
		active = true;
		pressure_fallback_remaining--;
	}

	if (!active)
		return false;

	bump_global_stat(9, 1);
	return true;
}

static __always_inline void record_shared_wait_start(struct task_struct *p,
						     __u64 now)
{
	struct flow_task_key key = {};

	if (!default_shared_wait_pressure_ns)
		return;
	if (!p)
		return;

	key.pid = p->pid;
	bpf_map_update_elem(&shared_wait_start, &key, &now, BPF_ANY);
}

static __always_inline void clear_shared_wait_start(struct task_struct *p)
{
	struct flow_task_key key = {};

	if (!p)
		return;

	key.pid = p->pid;
	bpf_map_delete_elem(&shared_wait_start, &key);
}

static __always_inline void maybe_open_shared_wait_pressure(struct task_struct *p,
							    __u64 now)
{
	struct flow_task_key key = {};
	__u64 *start;
	__u64 wait_ns;

	if (!default_shared_wait_pressure_ns)
		return;
	if (!p)
		return;

	key.pid = p->pid;
	start = bpf_map_lookup_elem(&shared_wait_start, &key);
	if (!start)
		return;

	if (time_after(now, *start)) {
		wait_ns = now - *start;
		if (wait_ns >= default_shared_wait_pressure_ns) {
			bump_global_stat(14, 1);
			bump_global_stat(15, wait_ns);
			open_pressure_epoch(now, bpf_get_smp_processor_id());
		}
	}
	bpf_map_delete_elem(&shared_wait_start, &key);
}

static __always_inline bool reserve_frontier_width(struct task_struct *p,
						   struct flow_task_ctx *ctx,
						   struct flow_session_policy *policy,
						   __u32 effective_width)
{
	struct flow_task_key key = { .pid = p->pid };
	struct flow_task_key parent_key = { .pid = 0 };
	struct flow_session_stats *s;
	__u64 *active_sid;
	__u64 *parent_sid;
	__u64 old_active;
	__u64 sid;

	if (!ctx || !policy)
		return false;
	if (!(policy->flags & FLOWCAUSAL_POLICY_WIDTH_CTL))
		return true;
	if (!effective_width)
		effective_width = policy->max_frontier_width;
	if (!effective_width)
		return true;

	active_sid = bpf_map_lookup_elem(&width_active, &key);
	if (active_sid)
		return true;

	if ((ctx->flags & FLOWCAUSAL_TASK_PROPAGATE) && ctx->parent_pid) {
		parent_key.pid = ctx->parent_pid;
		parent_sid = bpf_map_lookup_elem(&width_active, &parent_key);
		if (parent_sid && *parent_sid == ctx->session_id)
			return true;
	}

	s = lookup_or_create_session_stats(ctx->session_id);
	if (!s)
		return false;

	old_active = __sync_fetch_and_add(&s->frontier_inflight, 1);
	if (old_active >= effective_width) {
		__sync_fetch_and_sub(&s->frontier_inflight, 1);
		bump_session_stat(ctx->session_id, 3, 1);
		if (default_adaptive_width)
			bump_global_stat(FLOW_GSTAT_ADAPTIVE_WIDTH_HELD_ENQUEUES,
					 1);
		return false;
	}

	sid = ctx->session_id;
	if (bpf_map_update_elem(&width_active, &key, &sid, BPF_ANY)) {
		__sync_fetch_and_sub(&s->frontier_inflight, 1);
		return false;
	}
	return true;
}

static __always_inline void release_frontier_width(struct task_struct *p)
{
	struct flow_task_key key = { .pid = p->pid };
	struct flow_session_stats *s;
	__u64 *sid;
	__u64 sid_val;

	sid = bpf_map_lookup_elem(&width_active, &key);
	if (!sid)
		return;

	sid_val = *sid;
	s = bpf_map_lookup_elem(&session_stats, &sid_val);
	if (s && s->frontier_inflight)
		__sync_fetch_and_sub(&s->frontier_inflight, 1);
	bump_session_stat(sid_val, 8, 1);
	bpf_map_delete_elem(&width_active, &key);
}

static __always_inline bool task_ignores_boost_budget(struct flow_task_ctx *ctx,
						      struct flow_session_policy *p)
{
	__u64 now;

	if (!ctx || !p)
		return false;
	if ((FLOWCAUSAL_DEFAULT_ROOT_COPY_UNBUDGET ||
	     FLOWCAUSAL_DEFAULT_PACKAGE_ROOT_COPY_UNBUDGET) &&
	    (ctx->flags & FLOWCAUSAL_TASK_ROOT) &&
	    !(ctx->flags & FLOWCAUSAL_TASK_ROOT_STAGE_STARTED)) {
		if (FLOWCAUSAL_DEFAULT_ROOT_COPY_UNBUDGET)
			return true;
		if (FLOWCAUSAL_DEFAULT_PACKAGE_ROOT_COPY_UNBUDGET &&
		    p->anchor_cpu == 5)
			return true;
	}
	if (FLOWCAUSAL_DEFAULT_CXX_UNBUDGET &&
	    p->anchor_cpu == 0 &&
	    (ctx->flags & FLOWCAUSAL_TASK_STAGE_BUILD_WIDE))
		return true;
	if (FLOWCAUSAL_DEFAULT_PACKAGE_UNBUDGET &&
	    p->anchor_cpu == 5 &&
	    (ctx->flags & FLOWCAUSAL_TASK_STAGE_BUILD_WIDE))
		return true;
	if (FLOWCAUSAL_DEFAULT_PACKAGE_START_RESCUE_NS &&
	    scheduler_start_ns &&
	    p->anchor_cpu == 5 &&
	    (ctx->flags & FLOWCAUSAL_TASK_STAGE_BUILD_WIDE)) {
		now = bpf_ktime_get_ns();
		if (time_before(now, scheduler_start_ns +
				     FLOWCAUSAL_DEFAULT_PACKAGE_START_RESCUE_NS))
			return true;
	}
	if (FLOWCAUSAL_DEFAULT_PACKAGE_RESCUE_AFTER_CXX_ROOT &&
	    FLOWCAUSAL_DEFAULT_PACKAGE_RESCUE_UNBUDGET &&
	    p->anchor_cpu == 5 &&
	    (ctx->flags & FLOWCAUSAL_TASK_STAGE_BUILD_WIDE)) {
		now = bpf_ktime_get_ns();
		if (package_rescue_active_now(now) &&
		    !py_test_protect_active_now(now))
			return true;
	}
	return false;
}

static __always_inline __u64 task_boost_budget_ns(struct flow_task_ctx *ctx,
						  struct flow_session_policy *p)
{
	if (!p)
		return 0;
	if (FLOWCAUSAL_DEFAULT_PACKAGE_ROOT_COPY_BOOST_BUDGET_NS &&
	    package_root_copy_phase(ctx, p))
		return FLOWCAUSAL_DEFAULT_PACKAGE_ROOT_COPY_BOOST_BUDGET_NS;
	if (FLOWCAUSAL_DEFAULT_CXX_BOOST_BUDGET_NS &&
	    ctx && p->anchor_cpu == 0 &&
	    (ctx->flags & FLOWCAUSAL_TASK_STAGE_BUILD_WIDE))
		return FLOWCAUSAL_DEFAULT_CXX_BOOST_BUDGET_NS;
	return p->boost_budget_ns;
}

static __always_inline bool policy_admits_frontier(struct flow_task_ctx *ctx,
						   struct flow_session_policy *p,
						   __u64 now)
{
	__u64 budget_ns;

	if (!ctx || !p)
		return false;
	if (root_copy_phase_shared(ctx, p) ||
	    root_copy_phase_local(ctx, p))
		return false;
	if (!root_copy_single_owner_admits(ctx))
		return false;
	if (FLOWCAUSAL_DEFAULT_PACKAGE_ROOT_NO_FRONTIER &&
	    (ctx->flags & FLOWCAUSAL_TASK_ROOT) &&
	    p->anchor_cpu == 5)
		return false;
	if (!(ctx->flags & FLOWCAUSAL_TASK_FRONTIER))
		return false;
	if (ctx->flags & FLOWCAUSAL_TASK_OBSERVE_ONLY)
		return false;
	if (!(p->flags & FLOWCAUSAL_POLICY_ENABLE))
		return false;
	if (p->flags & FLOWCAUSAL_POLICY_FALLBACK)
		return false;
	if (p->expires_ns && time_after(now, p->expires_ns))
		return false;
	if (ctx->confidence < p->min_confidence)
		return false;
	budget_ns = task_boost_budget_ns(ctx, p);
	if (!task_ignores_boost_budget(ctx, p) &&
	    budget_ns && p->budget_used_ns >= budget_ns)
		return false;
	return true;
}

static __always_inline void refresh_budget_window(__u64 session_id,
						  struct flow_session_policy *p,
						  __u64 now)
{
	if (!p)
		return;
	if (!(p->flags & FLOWCAUSAL_POLICY_ENABLE))
		return;
	if (!p->window_ns)
		return;

	if (!p->budget_window_start_ns ||
	    now - p->budget_window_start_ns >= p->window_ns) {
		p->budget_window_start_ns = now;
		p->budget_used_ns = 0;
		bpf_map_update_elem(&session_policy, &session_id, p, BPF_ANY);
	}
}

static __always_inline __u64 bounded_vtime_boost(struct flow_task_ctx *ctx,
						 struct flow_session_policy *p)
{
	__u64 boost;

	if (!ctx || !p)
		return 0;

	boost = p->max_vtime_boost;
	if (!boost)
		boost = default_max_vtime_boost;

	/* Confidence scales the short-term preference without changing the
	 * long-term cgroup contract.
	 */
	boost = boost * clamp_confidence(ctx->confidence) / 100;
	if (boost > SCX_SLICE_DFL)
		boost = SCX_SLICE_DFL;
	return boost;
}

static __always_inline bool task_is_compile_chain(struct task_struct *p,
						  struct flow_task_ctx *ctx,
						  struct flow_session_policy *policy)
{
	if (!default_compile_chain_boost)
		return false;
	return task_matches_compile_chain(p, ctx, policy);
}

static __always_inline __u64 maybe_compile_chain_boost(struct task_struct *p,
						       struct flow_task_ctx *ctx,
						       struct flow_session_policy *policy,
						       __u64 boost)
{
	if (!task_is_compile_chain(p, ctx, policy))
		return boost;
	if (default_compile_chain_boost_ns > boost)
		boost = default_compile_chain_boost_ns;
	if (boost > SCX_SLICE_DFL)
		boost = SCX_SLICE_DFL;
	return boost;
}

static __always_inline __u64 deadline_vtime(struct task_struct *p, __u64 vtime)
{
	__u64 weight;
	__u64 dl;

	if (!default_deadline_vtime || !p)
		return vtime;

	weight = p->scx.weight ? p->scx.weight : 1;
	dl = (SCX_SLICE_DFL * 100) / weight;
	return vtime + dl;
}

static __always_inline void update_enqueue_ts(struct task_struct *p)
{
	struct flow_task_key key = { .pid = p->pid };
	struct flow_task_ctx *ctx;

	ctx = bpf_map_lookup_elem(&task_ctx, &key);
	if (!ctx)
		return;

	ctx->enqueue_ts_ns = bpf_ktime_get_ns();
	ctx->last_update_ns = ctx->enqueue_ts_ns;
}

s32 BPF_STRUCT_OPS(flowcausal_select_cpu, struct task_struct *p, s32 prev_cpu,
		   __u64 wake_flags)
{
	struct flow_task_key key = { .pid = p->pid };
	struct flow_task_ctx *tctx;
	struct flow_session_policy policy;
	bool package_rescue_locality = false;
	bool is_idle = false;
	s32 cpu;

	tctx = bpf_map_lookup_elem(&task_ctx, &key);
	if (tctx)
		package_rescue_locality = package_rescue_all_locality_ctx(tctx);
	if (tctx && (tctx->flags & FLOWCAUSAL_TASK_LOCALITY) &&
	    (package_rescue_locality ||
	     !(tctx->flags & (FLOWCAUSAL_TASK_STAGE_PACKAGE_BG |
			      FLOWCAUSAL_TASK_STAGE_INSPECT))) &&
	    (!default_locality_frontier_only ||
	     (tctx->flags & FLOWCAUSAL_TASK_FRONTIER) ||
	     package_rescue_locality)) {
		policy = lookup_policy_or_default_ctx(tctx);
		if ((policy.flags & FLOWCAUSAL_POLICY_LOCALITY) &&
		    policy.anchor_cpu >= 0) {
			s32 locality_cpu = policy_anchor_span_cpu(p, &policy,
								 tctx, prev_cpu);

			if (default_locality_remembered_first ||
			    (FLOWCAUSAL_DEFAULT_PY_TEST_REMEMBERED_FIRST &&
			     policy.anchor_cpu == 4 &&
			     (tctx->flags & FLOWCAUSAL_TASK_STAGE_TEST_TAIL))) {
				s32 remembered = remembered_task_cpu(p);

				if (cpu_in_policy_anchor_span(p, &policy, tctx,
							      remembered) &&
				    task_can_run_on_sched_cpu(p, remembered))
					locality_cpu = remembered;
			}

			cpu = scx_bpf_select_cpu_dfl(p, locality_cpu,
						     wake_flags, &is_idle);
			if (!cpu_in_policy_anchor_span(p, &policy, tctx, cpu) &&
			    task_can_run_on_sched_cpu(p, locality_cpu))
				cpu = locality_cpu;
			remember_task_cpu(p, cpu);
			return cpu;
		}
	}

	if (default_mode_aware && tctx) {
		__u32 mode = infer_task_mode(tctx, p);
		s32 remembered = remembered_task_cpu(p);

		if (mode == FLOWCAUSAL_TASK_MODE_COMPUTE &&
		    task_can_run_on_sched_cpu(p, remembered))
			prev_cpu = remembered;
	}

	cpu = scx_bpf_select_cpu_dfl(p, prev_cpu, wake_flags, &is_idle);
	remember_task_cpu(p, cpu);
	if (is_idle && !tctx) {
		__u64 slice = shared_slice_ns();

		remember_task_slice(p, slice);
		scx_bpf_dsq_insert(p, SCX_DSQ_LOCAL, slice, 0);
	}
	return cpu;
}

void BPF_STRUCT_OPS(flowcausal_enqueue, struct task_struct *p, __u64 enq_flags)
{
	struct flow_task_key key = { .pid = p->pid };
	struct flow_task_ctx *tctx;
	struct flow_session_policy policy;
	__u64 now = bpf_ktime_get_ns();
	__u64 vtime = p->scx.dsq_vtime;
	__u64 boost = 0;
	__u32 event_flags;
	__u32 mode = FLOWCAUSAL_TASK_MODE_UNKNOWN;
	__u32 effective_width = 0;
	bool converge_tail = false;
	bool cxx_first_suppressed = false;
	bool admitted = false;

	tctx = bpf_map_lookup_elem(&task_ctx, &key);
	if (!tctx) {
		bool latency_wakeup = shared_wakeup_preempt_candidate(p, true, now);
		__u64 dsq_id = latency_wakeup ? FLOWCAUSAL_LATENCY_DSQ :
					       FLOWCAUSAL_SHARED_DSQ;
		__u64 slice = latency_wakeup ? shared_wakeup_slice_ns() :
					       shared_slice_ns();
			__u64 flags = latency_wakeup ?
				      shared_wakeup_preempt_flags(p, enq_flags) :
				      enq_flags;

			bump_global_stat(FLOW_GSTAT_UNTRACKED_SHARED_PATH_ENQUEUES, 1);
			record_shared_wait_start(p, now);
			if (!latency_wakeup &&
			    enqueue_shared_local(p, slice, flags, false, false))
				return;
			if (!latency_wakeup &&
			    enqueue_shared_percpu(p, slice, vtime, flags, false))
				return;

			remember_task_slice(p, slice);
			if (fifo_sched)
				scx_bpf_dsq_insert(p, dsq_id, slice, flags);
			else
				scx_bpf_dsq_insert_vtime(p, dsq_id, slice,
							 latency_wakeup ? vtime :
							 deadline_vtime(p, vtime),
							 flags);
			if (latency_wakeup) {
				bump_global_stat(FLOW_GSTAT_LATENCY_DSQ_ENQUEUES, 1);
			} else {
				bump_global_stat(FLOW_GSTAT_SHARED_GLOBAL_ENQUEUES, 1);
				bump_global_stat(FLOW_GSTAT_SHARED_GLOBAL_UNTRACKED_ENQUEUES, 1);
			}
			bump_mode_stat(FLOWCAUSAL_TASK_MODE_INTERACTIVE, true, false);
			return;
		}

	if (consume_root_child_wakeup(p, &key, tctx, enq_flags))
		return;

	if (consume_parent_exit_latency(p, &key, tctx, enq_flags, vtime))
		return;

	tctx->enqueue_ts_ns = now;
	mark_session_queued(p, tctx);
	policy = lookup_policy_or_default_ctx(tctx);
	refresh_budget_window(tctx->session_id, &policy, now);
	apply_bpf_auto_classification(p, tctx, &policy, now);
	bump_session_stat(tctx->session_id, 0, 1);

	event_flags = tctx->flags;
	mode = infer_task_mode(tctx, p);
	cxx_first_suppressed = cxx_first_suppresses_task(tctx, &policy, now);
	if (FLOWCAUSAL_DEFAULT_CXX_FIRST_SUPPRESS_TO_BACKGROUND &&
	    cxx_first_suppressed) {
		tctx->flags &= ~(FLOWCAUSAL_TASK_FRONTIER |
				 FLOWCAUSAL_TASK_COHORT_TAIL |
				 FLOWCAUSAL_TASK_PROPAGATE |
				 FLOWCAUSAL_TASK_CONVERGE_TAIL);
		tctx->flags |= FLOWCAUSAL_TASK_BACKGROUND;
		mode = FLOWCAUSAL_TASK_MODE_BACKGROUND;
		event_flags = tctx->flags;
	}
	if (default_converge)
		tctx->flags &= ~FLOWCAUSAL_TASK_CONVERGE_TAIL;
	bump_mode_stat(mode, true, false);
	if (consume_py_test_runtime_tail(p, tctx, &policy, enq_flags, vtime))
		return;
	if (consume_root_copy_wakeup(p, tctx, enq_flags, vtime, now))
		return;
	if (consume_package_root_copy_wakeup(p, tctx, &policy, enq_flags,
					     vtime, now))
		return;
	if (consume_package_root_copy_latency(p, tctx, &policy, enq_flags,
					      vtime))
		return;
	if (root_copy_phase_local(tctx, &policy)) {
		__u64 slice = shared_slice_ns();
		__u64 flags = shared_enqueue_flags(p, enq_flags, false, now);

		if (enqueue_root_copy_local(p, tctx, &policy, slice, flags))
			return;
	}
	if (FLOWCAUSAL_DEFAULT_ROOT_COPY_DSQ && root_copy_phase(tctx)) {
		__u64 slice = tracked_shared_slice_ns(tctx, &policy);
		__u64 flags = shared_enqueue_flags(p, enq_flags, false, now);

		bump_global_stat(FLOW_GSTAT_TRACKED_SHARED_PATH_ENQUEUES, 1);
		if (tctx->flags & FLOWCAUSAL_TASK_FRONTIER) {
			bump_session_stat(tctx->session_id, 2, 1);
			emit_event(FLOW_EVT_FALLBACK, p, tctx, event_flags);
		}
		if (enqueue_root_copy_dsq(p, tctx, &policy, slice, vtime, flags))
			return;
	}
	if (mode == FLOWCAUSAL_TASK_MODE_BACKGROUND) {
		__u64 slice = shared_slice_ns();
		__u64 flags = shared_enqueue_flags(p, enq_flags, false, now);

		if (enqueue_background(p, tctx, slice, vtime, flags))
			return;
	}
	if (default_mode_aware && mode == FLOWCAUSAL_TASK_MODE_INTERACTIVE &&
	    shared_wakeup_preempt_candidate(p, true, now)) {
		__u64 slice = shared_wakeup_slice_ns();
		__u64 flags = shared_wakeup_preempt_flags(p, enq_flags);

		remember_task_slice(p, slice);
		if (fifo_sched)
			scx_bpf_dsq_insert(p, FLOWCAUSAL_LATENCY_DSQ, slice, flags);
		else
			scx_bpf_dsq_insert_vtime(p, FLOWCAUSAL_LATENCY_DSQ,
						 slice, vtime, flags);
		bump_global_stat(FLOW_GSTAT_LATENCY_DSQ_ENQUEUES, 1);
		return;
	}
	if (!cxx_first_suppressed)
		admitted = policy_admits_frontier(tctx, &policy, now);
	if (admitted) {
		__u64 budget_ns = task_boost_budget_ns(tctx, &policy);

		boost = bounded_vtime_boost(tctx, &policy);
		boost = maybe_compile_chain_boost(p, tctx, &policy, boost);
		if (!task_ignores_boost_budget(tctx, &policy) &&
		    budget_ns && boost &&
		    (policy.budget_used_ns >= budget_ns ||
		     boost > budget_ns - policy.budget_used_ns))
			admitted = false;
	}
	if (admitted && consume_pressure_fallback_slot(now, bpf_get_smp_processor_id())) {
		admitted = false;
		event_flags |= FLOWCAUSAL_TASK_PRESSURE_HELD;
	}
	if (admitted)
		effective_width = effective_frontier_width(tctx, &policy, mode,
							   now,
							   &converge_tail);
	if (admitted && !reserve_frontier_width(p, tctx, &policy,
						effective_width)) {
		admitted = false;
		event_flags |= FLOWCAUSAL_TASK_WIDTH_HELD;
	}
	if (admitted) {
		__u64 frontier_flags = frontier_wakeup_preempt_flags(p, enq_flags);

		if (boost > vtime)
			vtime = 0;
		else
			vtime -= boost;

		if (!task_ignores_boost_budget(tctx, &policy) &&
		    task_boost_budget_ns(tctx, &policy)) {
			__u64 sid = tctx->session_id;
			__u64 new_used = policy.budget_used_ns + boost;

			policy.budget_used_ns = new_used;
			bpf_map_update_elem(&session_policy, &sid,
					    &policy, BPF_ANY);
		}

			bump_session_stat(tctx->session_id, 1, 1);
			bump_global_stat(FLOW_GSTAT_FRONTIER_DSQ_ENQUEUES, 1);
			emit_event(FLOW_EVT_FRONTIER_ENQUEUE, p, tctx, tctx->flags);

		remember_task_slice(p, SCX_SLICE_DFL);
		if (critical_prefix_dsq_task(tctx, &policy)) {
			tctx->flags &= ~FLOWCAUSAL_TASK_CONVERGE_TAIL;
			if (fifo_sched)
				scx_bpf_dsq_insert(p, FLOWCAUSAL_CONVERGE_DSQ,
						   SCX_SLICE_DFL,
						   frontier_flags);
			else
				scx_bpf_dsq_insert_vtime(p,
							 FLOWCAUSAL_CONVERGE_DSQ,
							 SCX_SLICE_DFL, vtime,
							 frontier_flags);
			return;
		}
		if (default_converge_tail_dsq && converge_tail) {
			tctx->flags |= FLOWCAUSAL_TASK_CONVERGE_TAIL;
			bump_global_stat(FLOW_GSTAT_CONVERGE_DSQ_ENQUEUES, 1);
			if (fifo_sched)
				scx_bpf_dsq_insert(p, FLOWCAUSAL_CONVERGE_DSQ,
						   SCX_SLICE_DFL,
						   frontier_flags);
			else
				scx_bpf_dsq_insert_vtime(p,
							 FLOWCAUSAL_CONVERGE_DSQ,
							 SCX_SLICE_DFL, vtime,
							 frontier_flags);
			return;
		}
		tctx->flags &= ~FLOWCAUSAL_TASK_CONVERGE_TAIL;
		if (enqueue_frontier_local(p, tctx, &policy, frontier_flags))
			return;
		if (default_session_frontier_dsq) {
			__u32 slot = session_frontier_slot(tctx->session_id);

			bump_global_stat(FLOW_GSTAT_SESSION_FRONTIER_DSQ_ENQUEUES,
					 1);
			if (fifo_sched)
				scx_bpf_dsq_insert(p,
						   session_frontier_dsq_id(slot),
						   SCX_SLICE_DFL,
						   frontier_flags);
			else
				scx_bpf_dsq_insert_vtime(p,
							 session_frontier_dsq_id(slot),
							 SCX_SLICE_DFL, vtime,
							 frontier_flags);
			return;
		}
		if (fifo_sched)
			scx_bpf_dsq_insert(p, FLOWCAUSAL_FRONTIER_DSQ,
					   SCX_SLICE_DFL, frontier_flags);
		else
			scx_bpf_dsq_insert_vtime(p, FLOWCAUSAL_FRONTIER_DSQ,
						 SCX_SLICE_DFL, vtime,
						 frontier_flags);
		return;
	}

	if (consume_critical_wait_frontier_bypass(p, tctx, &policy,
						  enq_flags, event_flags))
		return;

	if (tctx->flags & FLOWCAUSAL_TASK_FRONTIER) {
		bump_session_stat(tctx->session_id, 2, 1);
		emit_event(FLOW_EVT_FALLBACK, p, tctx, event_flags);
	}

	if (time_before(vtime, vtime_now - SCX_SLICE_DFL))
		vtime = vtime_now - SCX_SLICE_DFL;

	{
		__u64 slice = tracked_shared_slice_ns(tctx, &policy);
		/*
		 * Tracked sandbox tasks that fall back to the shared DSQ remain
		 * part of the optimized workflow.  Do not let the co-runner
		 * wakeup-preempt shield turn stage-generic background work into
		 * a latency-sensitive victim.  Untracked tasks, such as an
		 * external sidecar, can still use the shield above.
			*/
		__u64 flags = shared_enqueue_flags(p, enq_flags, false, now);

		bump_global_stat(FLOW_GSTAT_TRACKED_SHARED_PATH_ENQUEUES, 1);
		if (package_rescue_fallback_percpu_ctx(tctx)) {
			s32 cpu = package_rescue_fallback_cpu(p, tctx,
							      scx_bpf_task_cpu(p));

			if (task_can_use_percpu_dsq(p, cpu)) {
				remember_task_cpu(p, cpu);
				if (enqueue_shared_percpu(p, slice, vtime,
							  flags, true))
					return;
			}
		}
		if (mode == FLOWCAUSAL_TASK_MODE_COMPUTE) {
			if (enqueue_shared_percpu(p, slice, vtime, flags, true))
				return;
			if (enqueue_shared_local(p, slice, flags, true,
						 tctx->flags & FLOWCAUSAL_TASK_FRONTIER))
				return;
		} else {
			if (enqueue_shared_local(p, slice, flags, true,
						 tctx->flags & FLOWCAUSAL_TASK_FRONTIER))
				return;
			if (enqueue_shared_percpu(p, slice, vtime, flags, true))
				return;
		}

		remember_task_slice(p, slice);
		if (fifo_sched)
			scx_bpf_dsq_insert(p, FLOWCAUSAL_SHARED_DSQ, slice, flags);
		else
			scx_bpf_dsq_insert_vtime(p, FLOWCAUSAL_SHARED_DSQ,
						 slice, deadline_vtime(p, vtime),
						 flags);
		bump_global_stat(FLOW_GSTAT_SHARED_GLOBAL_ENQUEUES, 1);
		bump_global_stat(FLOW_GSTAT_SHARED_GLOBAL_TRACKED_ENQUEUES, 1);
	}
	}

void BPF_STRUCT_OPS(flowcausal_dispatch, s32 cpu, struct task_struct *prev)
{
	__u64 now = bpf_ktime_get_ns();

	/*
	 * Frontier-first dispatch is useful only while it remains a bounded bias.
	 * After a short streak of frontier dispatches, try the shared DSQ first so
	 * unhinted/victim tasks can make progress.  If this probe discovers shared
	 * backlog, optionally keep a small shared-first cooldown window.  If shared
	 * is empty, stop cooling down and dispatch from the frontier path
	 * immediately.
	 *
	 * A separate soft windowed dispatch budget can also force a shared-first
	 * probe once the frontier path consumed enough dispatches in the current
	 * wall-clock window.  A budget miss never idles the CPU; it only proves
	 * that no shared work was available at the budget boundary.
	 */
	if (shared_wakeup_preempt_dispatch_pending) {
		shared_wakeup_preempt_dispatch_pending = false;
		if (scx_bpf_dsq_move_to_local(FLOWCAUSAL_LATENCY_DSQ)) {
			frontier_dispatch_streak = 0;
			bump_global_stat(1, 1);
			bump_global_stat(2, 1);
			bump_global_stat(20, 1);
			open_pressure_epoch(now, cpu);
			note_foreground_dispatch();
			return;
		}
		bump_global_stat(21, 1);
	}

	if (scx_bpf_dsq_move_to_local(FLOWCAUSAL_LATENCY_DSQ)) {
		frontier_dispatch_streak = 0;
		bump_global_stat(1, 1);
		bump_global_stat(2, 1);
		open_pressure_epoch(now, cpu);
		note_foreground_dispatch();
		return;
	}

	if (FLOWCAUSAL_DEFAULT_PARENT_EXIT_CLEANUP_DSQ &&
	    scx_bpf_dsq_move_to_local(FLOWCAUSAL_CLEANUP_DSQ)) {
		frontier_dispatch_streak = 0;
		bump_global_stat(1, 1);
		bump_global_stat(2, 1);
		open_pressure_epoch(now, cpu);
		note_foreground_dispatch();
		return;
	}

	if (FLOWCAUSAL_DEFAULT_PACKAGE_RESCUE_SHARED_PROBE &&
	    package_rescue_active_now(now) &&
	    (!FLOWCAUSAL_DEFAULT_PACKAGE_RESCUE_SHARED_PROBE_MIN_STREAK ||
	     frontier_dispatch_streak >=
		     FLOWCAUSAL_DEFAULT_PACKAGE_RESCUE_SHARED_PROBE_MIN_STREAK) &&
	    move_shared_to_local(cpu)) {
		frontier_dispatch_streak = 0;
		bump_global_stat(17, 1);
		note_foreground_dispatch();
		return;
	}

	if (default_converge_tail_dsq &&
	    scx_bpf_dsq_move_to_local(FLOWCAUSAL_CONVERGE_DSQ)) {
		consume_frontier_dispatch_budget(now);
		if (default_frontier_dispatch_quota &&
		    frontier_dispatch_streak < 0xffffffffU)
			frontier_dispatch_streak++;
		bump_global_stat(FLOW_GSTAT_CONVERGE_DISPATCHES, 1);
		bump_mode_stat(FLOWCAUSAL_TASK_MODE_TAIL, false, true);
		note_foreground_dispatch();
		return;
	}

	if (move_session_frontier_to_local()) {
		consume_frontier_dispatch_budget(now);
		if (default_frontier_dispatch_quota &&
		    frontier_dispatch_streak < 0xffffffffU)
			frontier_dispatch_streak++;
		bump_global_stat(0, 1);
		bump_mode_stat(FLOWCAUSAL_TASK_MODE_TAIL, false, true);
		note_foreground_dispatch();
		return;
	}

	if (maybe_dispatch_background_quota())
		return;

	if (maybe_dispatch_root_copy_quota(false))
		return;

	if (shared_guard_cooldown_remaining) {
		shared_guard_cooldown_remaining--;
		if (move_shared_to_local(cpu)) {
			frontier_dispatch_streak = 0;
			bump_global_stat(1, 1);
			bump_global_stat(2, 1);
			bump_global_stat(6, 1);
			open_pressure_epoch(now, cpu);
			note_foreground_dispatch();
			return;
		}
		shared_guard_cooldown_remaining = 0;
		bump_global_stat(7, 1);
	}

	if (default_frontier_dispatch_quota &&
	    frontier_dispatch_streak >= default_frontier_dispatch_quota) {
		bump_global_stat(3, 1);
		if (move_shared_to_local(cpu)) {
			frontier_dispatch_streak = 0;
			bump_global_stat(1, 1);
			bump_global_stat(2, 1);
			open_pressure_epoch(now, cpu);
			if (default_guard_cooldown_dispatches) {
				shared_guard_cooldown_remaining =
					default_guard_cooldown_dispatches;
				bump_global_stat(5, 1);
			}
			note_foreground_dispatch();
			return;
		}
		bump_global_stat(4, 1);
	}

	if (frontier_dispatch_budget_depleted(now)) {
		bump_global_stat(11, 1);
		if (move_shared_to_local(cpu)) {
			frontier_dispatch_streak = 0;
			bump_global_stat(1, 1);
			bump_global_stat(12, 1);
			open_pressure_epoch(now, cpu);
			note_foreground_dispatch();
			return;
		}
		bump_global_stat(13, 1);
	}

	if (pressure_dispatch_active(now, cpu)) {
		if (move_shared_to_local(cpu)) {
			frontier_dispatch_streak = 0;
			bump_global_stat(1, 1);
			bump_global_stat(17, 1);
			note_foreground_dispatch();
			return;
		}
		bump_global_stat(18, 1);
	}

	if (default_converge_tail_dsq &&
	    scx_bpf_dsq_move_to_local(FLOWCAUSAL_CONVERGE_DSQ)) {
		consume_frontier_dispatch_budget(now);
		if (default_frontier_dispatch_quota &&
		    frontier_dispatch_streak < 0xffffffffU)
			frontier_dispatch_streak++;
		bump_global_stat(FLOW_GSTAT_CONVERGE_DISPATCHES, 1);
		bump_mode_stat(FLOWCAUSAL_TASK_MODE_TAIL, false, true);
		note_foreground_dispatch();
		return;
	}

	if (move_session_frontier_to_local()) {
		consume_frontier_dispatch_budget(now);
		if (default_frontier_dispatch_quota &&
		    frontier_dispatch_streak < 0xffffffffU)
			frontier_dispatch_streak++;
		bump_global_stat(0, 1);
		bump_mode_stat(FLOWCAUSAL_TASK_MODE_TAIL, false, true);
		note_foreground_dispatch();
		return;
	}

	if (scx_bpf_dsq_move_to_local(FLOWCAUSAL_FRONTIER_DSQ)) {
		consume_frontier_dispatch_budget(now);
		if (default_frontier_dispatch_quota &&
		    frontier_dispatch_streak < 0xffffffffU)
			frontier_dispatch_streak++;
		bump_global_stat(0, 1);
		bump_mode_stat(FLOWCAUSAL_TASK_MODE_TAIL, false, true);
		note_foreground_dispatch();
		return;
	}
	if (move_shared_to_local(cpu)) {
		frontier_dispatch_streak = 0;
		bump_global_stat(1, 1);
		note_foreground_dispatch();
		return;
	}
	if (maybe_dispatch_root_copy_quota(true))
		return;
	if (default_background_dsq &&
	    scx_bpf_dsq_move_to_local(FLOWCAUSAL_BACKGROUND_DSQ)) {
		frontier_dispatch_streak = 0;
		background_dispatch_streak = 0;
		bump_global_stat(28, 1);
		bump_mode_stat(FLOWCAUSAL_TASK_MODE_BACKGROUND, false, true);
	}
}

void BPF_STRUCT_OPS(flowcausal_running, struct task_struct *p)
{
	struct flow_task_key key = { .pid = p->pid };
	struct flow_task_ctx *tctx;
	__u64 now = bpf_ktime_get_ns();

	if (time_before(vtime_now, p->scx.dsq_vtime))
		vtime_now = p->scx.dsq_vtime;

	tctx = bpf_map_lookup_elem(&task_ctx, &key);
	if (!tctx) {
		maybe_open_shared_wait_pressure(p, now);
		return;
	}
	mark_session_dequeued(p, tctx);
	clear_shared_wait_start(p);

	if (tctx->enqueue_ts_ns && time_after(now, tctx->enqueue_ts_ns)) {
		__u64 wait_ns = now - tctx->enqueue_ts_ns;

		tctx->runnable_wait_ns += wait_ns;
		bump_session_stat(tctx->session_id, 4, wait_ns);
		if (tctx->flags & FLOWCAUSAL_TASK_FRONTIER)
			bump_session_stat(tctx->session_id, 5, wait_ns);
	}
	tctx->running_since_ns = now;
	tctx->last_update_ns = now;
	emit_event(FLOW_EVT_SWITCH_IN, p, tctx, tctx->flags);
}

void BPF_STRUCT_OPS(flowcausal_stopping, struct task_struct *p, bool runnable)
{
	struct flow_task_key key = { .pid = p->pid };
	struct flow_sched_task_state *state;
	struct flow_task_ctx *tctx;
	__u64 now = bpf_ktime_get_ns();
	__u64 used = accounted_slice_used(p);

	p->scx.dsq_vtime += used * 100 / p->scx.weight;
	state = bpf_task_storage_get(&sched_task_state, p, 0,
				     BPF_LOCAL_STORAGE_GET_F_CREATE);
	if (state) {
		state->last_blocked = !runnable;
		state->blocked_since_ns = runnable ? 0 : now;
	}

	tctx = bpf_map_lookup_elem(&task_ctx, &key);
	if (!tctx) {
		release_frontier_width(p);
		return;
	}

	if (tctx->running_since_ns && time_after(now, tctx->running_since_ns)) {
		__u64 runtime_ns = now - tctx->running_since_ns;

		tctx->runtime_ns += runtime_ns;
		bump_session_stat(tctx->session_id, 6, runtime_ns);
		if (tctx->flags & FLOWCAUSAL_TASK_FRONTIER)
			bump_session_stat(tctx->session_id, 7, runtime_ns);
	}
	tctx->last_update_ns = now;
	release_frontier_width(p);
	emit_event(FLOW_EVT_SWITCH_OUT, p, tctx, tctx->flags);
}

void BPF_STRUCT_OPS(flowcausal_enable, struct task_struct *p)
{
	p->scx.dsq_vtime = vtime_now;
	update_enqueue_ts(p);
}

s32 BPF_STRUCT_OPS_SLEEPABLE(flowcausal_init)
{
	__u32 nr_cpu_ids;
	__u32 cpu;
	s32 ret;

	scheduler_start_ns = bpf_ktime_get_ns();

	ret = scx_bpf_create_dsq(FLOWCAUSAL_SHARED_DSQ, -1);
	if (ret)
		return ret;
	ret = scx_bpf_create_dsq(FLOWCAUSAL_FRONTIER_DSQ, -1);
	if (ret)
		return ret;
	ret = scx_bpf_create_dsq(FLOWCAUSAL_LATENCY_DSQ, -1);
	if (ret)
		return ret;
	ret = scx_bpf_create_dsq(FLOWCAUSAL_BACKGROUND_DSQ, -1);
	if (ret)
		return ret;
	ret = scx_bpf_create_dsq(FLOWCAUSAL_CONVERGE_DSQ, -1);
	if (ret)
		return ret;
	ret = scx_bpf_create_dsq(FLOWCAUSAL_CLEANUP_DSQ, -1);
	if (ret)
		return ret;
	ret = scx_bpf_create_dsq(FLOWCAUSAL_ROOT_COPY_DSQ, -1);
	if (ret)
		return ret;

	if (default_session_frontier_dsq) {
		__u32 slot;

		bpf_for(slot, 0, FLOWCAUSAL_SESSION_FRONTIER_DSQ_SLOTS) {
			ret = scx_bpf_create_dsq(session_frontier_dsq_id(slot),
						 -1);
			if (ret)
				return ret;
		}
	}

	if (!default_shared_percpu_dsq)
		return 0;

	nr_cpu_ids = scx_bpf_nr_cpu_ids();
	if (nr_cpu_ids > FLOWCAUSAL_MAX_CPUS)
		nr_cpu_ids = FLOWCAUSAL_MAX_CPUS;

	bpf_for(cpu, 0, nr_cpu_ids) {
		ret = scx_bpf_create_dsq(shared_percpu_dsq_id(cpu), -1);
		if (ret)
			return ret;
	}
	return 0;
}

void BPF_STRUCT_OPS(flowcausal_exit, struct scx_exit_info *ei)
{
	UEI_RECORD(uei, ei);
}

SEC("tp_btf/sched_process_fork")
int BPF_PROG(flowcausal_on_fork, struct task_struct *parent_task,
	     struct task_struct *child_task)
{
	struct flow_task_key child_key = {};
	struct flow_task_key parent_key = {};
	struct flow_task_ctx child_ctx = {};
	struct flow_session_policy policy;
	struct flow_task_ctx *parent;
	__u32 confidence;
	__u32 parent_pid;
	__u32 child_pid;
	__u64 now;

	if (!parent_task || !child_task)
		return 0;

	parent_pid = parent_task->pid;
	child_pid = child_task->pid;
	parent_key.pid = parent_pid;
	parent = bpf_map_lookup_elem(&task_ctx, &parent_key);
	if (!parent)
		return 0;
	if (parent->session_id == FLOWCAUSAL_PARENT_EXIT_SESSION_ID)
		return 0;
	if (parent->flags & FLOWCAUSAL_TASK_ROOT) {
		root_copy_single_owner_release(parent);
		parent->flags |= FLOWCAUSAL_TASK_ROOT_STAGE_STARTED;
		if (parent->anchor_cpu == 0)
			cxx_root_stage_started = true;
		parent->last_update_ns = bpf_ktime_get_ns();
		root_active_child_inc(&parent_key);
	}

	child_ctx = *parent;
	now = bpf_ktime_get_ns();
	child_ctx.enqueue_ts_ns = 0;
	child_ctx.running_since_ns = 0;
	child_ctx.runnable_wait_ns = 0;
	child_ctx.runtime_ns = 0;
	child_ctx.last_update_ns = now;
	child_ctx.tgid = child_task->tgid;
	child_ctx.parent_pid = parent_pid;
	child_ctx.flags &= (FLOWCAUSAL_TASK_LOCALITY |
			    FLOWCAUSAL_TASK_STAGE_MASK |
			    FLOWCAUSAL_TASK_PACKAGE_BG_MARK);
	if (parent->flags & FLOWCAUSAL_TASK_C_ASSIST)
		child_ctx.flags |= FLOWCAUSAL_TASK_C_ASSIST;
	if (parent->flags & FLOWCAUSAL_TASK_BPF_AUTO)
		child_ctx.flags |= FLOWCAUSAL_TASK_BPF_AUTO;
	child_ctx.propagation_depth = 0;
	if ((parent->flags & FLOWCAUSAL_TASK_PROPAGATE) &&
	    parent->propagation_depth > 0) {
		child_ctx.flags |= FLOWCAUSAL_TASK_PROPAGATE;
		if (parent->flags & FLOWCAUSAL_TASK_STAGE_PACKAGE_BG) {
			child_ctx.flags |= FLOWCAUSAL_TASK_BACKGROUND;
			child_ctx.flags &= ~(FLOWCAUSAL_TASK_FRONTIER |
					     FLOWCAUSAL_TASK_COHORT_TAIL |
					     FLOWCAUSAL_TASK_CONVERGE_TAIL);
		} else if (parent->flags & FLOWCAUSAL_TASK_STAGE_BUILD_WIDE) {
			child_ctx.flags |= FLOWCAUSAL_TASK_FRONTIER;
			child_ctx.flags &= ~(FLOWCAUSAL_TASK_COHORT_TAIL |
					     FLOWCAUSAL_TASK_CONVERGE_TAIL |
					     FLOWCAUSAL_TASK_BACKGROUND);
		} else {
			child_ctx.flags |= FLOWCAUSAL_TASK_FRONTIER |
					   FLOWCAUSAL_TASK_COHORT_TAIL;
			if (parent->flags & FLOWCAUSAL_TASK_STAGE_TEST_TAIL)
				child_ctx.flags |= FLOWCAUSAL_TASK_CONVERGE_TAIL;
		}
		child_ctx.propagation_depth = parent->propagation_depth - 1;
		policy = lookup_policy_or_default_ctx(&child_ctx);
		confidence = clamp_confidence(child_ctx.confidence);
		if (confidence < policy.min_confidence)
			confidence = policy.min_confidence;
		child_ctx.confidence = clamp_confidence(confidence);
	}
	classify_root_direct_child_by_seq(&child_ctx, parent, &parent_key);
	mark_package_bg_direct_child_by_seq(&child_ctx, parent, &parent_key);
	classify_package_direct_child_by_seq(&child_ctx, parent, &parent_key);
	classify_package_test_direct_child_by_seq(&child_ctx, parent, &parent_key);
	mark_cxx_third_direct_child_by_seq(&child_ctx, parent, &parent_key);
	mark_critical_direct_child_by_seq(&child_ctx, parent, &parent_key);
	child_key.pid = child_pid;
	bpf_map_update_elem(&task_ctx, &child_key, &child_ctx, BPF_ANY);
	if (default_converge && child_ctx.session_id) {
		struct flow_session_runtime *sr;

		sr = lookup_or_create_session_runtime(child_ctx.session_id);
		if (sr)
			__sync_fetch_and_add(&sr->forked_tasks, 1);
	}
	emit_event(FLOW_EVT_FORK, child_task, &child_ctx, child_ctx.flags);
	return 0;
}

SEC("tp/sched/sched_process_exec")
int flowcausal_on_exec(void *ctx)
{
	__u64 pid_tgid = bpf_get_current_pid_tgid();
	struct flow_task_key key = { .pid = (__u32)pid_tgid };
	struct flow_task_ctx *task;

	task = bpf_map_lookup_elem(&task_ctx, &key);
	if (task)
		emit_event(FLOW_EVT_EXEC, NULL, task, task->flags);
	if (task) {
		/* exec swaps in a new executable image; let the next enqueue
		 * reclassify from fresh comm/runtime evidence instead of
		 * inheriting a stale mode bit from the previous phase.
		 */
		if (default_bpf_auto_frontier &&
		    (task->flags & FLOWCAUSAL_TASK_BPF_AUTO))
			clear_auto_flags(task);
		task->flags &= ~FLOWCAUSAL_TASK_C_ASSIST;
		task->flags &= ~FLOWCAUSAL_TASK_MODE_MASK;
		task->last_update_ns = bpf_ktime_get_ns();
	}
	return 0;
}

SEC("tp/sched/sched_process_exit")
int flowcausal_on_exit(void *ctx)
{
	__u64 pid_tgid = bpf_get_current_pid_tgid();
	struct flow_task_key key = { .pid = (__u32)pid_tgid };
	struct flow_task_ctx *task;

	task = bpf_map_lookup_elem(&task_ctx, &key);
	if (task) {
		bool root_child_zero_ready = root_active_child_dec_to_zero(task);

		emit_event(FLOW_EVT_EXIT, NULL, task, task->flags);
		root_copy_single_owner_release(task);
		if (FLOWCAUSAL_DEFAULT_ROOT_CHILD_WAKEUP && task->parent_pid) {
			struct flow_task_key parent_key = { .pid = task->parent_pid };
			struct flow_task_ctx *parent;

			parent = bpf_map_lookup_elem(&task_ctx, &parent_key);
			if (parent &&
			    parent->session_id != FLOWCAUSAL_PARENT_EXIT_SESSION_ID &&
			    (parent->flags & FLOWCAUSAL_TASK_ROOT) &&
			    root_child_zero_ready &&
			    (FLOWCAUSAL_DEFAULT_ROOT_CHILD_WAKEUP_ANCHOR_CPU < 0 ||
			     parent->anchor_cpu ==
			     FLOWCAUSAL_DEFAULT_ROOT_CHILD_WAKEUP_ANCHOR_CPU)) {
				__u64 until = bpf_ktime_get_ns() +
					      FLOWCAUSAL_DEFAULT_ROOT_CHILD_WAKEUP_NS;

				bpf_map_update_elem(&root_child_wakeup_until,
						    &parent_key, &until, BPF_ANY);
			}
		}
			if (FLOWCAUSAL_DEFAULT_PACKAGE_RESCUE_AFTER_CXX_BUILD_CHILD &&
			    task->anchor_cpu == 0 &&
			    !(task->flags & FLOWCAUSAL_TASK_ROOT) &&
			    (task->flags & FLOWCAUSAL_TASK_STAGE_BUILD_WIDE) &&
			    task_is_direct_child_of_root_anchor(task, 0))
				package_rescue_until_ns =
					bpf_ktime_get_ns() + FLOWCAUSAL_DEFAULT_PACKAGE_RESCUE_NS;
			if (FLOWCAUSAL_DEFAULT_PACKAGE_RESCUE_AFTER_CXX_THIRD_CHILD &&
			    task->anchor_cpu == 0 &&
			    !(task->flags & FLOWCAUSAL_TASK_ROOT) &&
			    (task->flags & FLOWCAUSAL_TASK_ROOT_DIRECT_THIRD))
				package_rescue_until_ns =
					bpf_ktime_get_ns() + FLOWCAUSAL_DEFAULT_PACKAGE_RESCUE_NS;
			if ((FLOWCAUSAL_DEFAULT_PACKAGE_RESCUE_AFTER_CXX_ROOT ||
			     FLOWCAUSAL_DEFAULT_PACKAGE_TAIL_LOCALITY_AFTER_PY_ROOT) &&
			    (task->flags & FLOWCAUSAL_TASK_ROOT)) {
			__u64 rescue_now = bpf_ktime_get_ns();

			if (FLOWCAUSAL_DEFAULT_PACKAGE_RESCUE_AFTER_CXX_ROOT &&
			    task->anchor_cpu == 0)
				package_rescue_until_ns =
					rescue_now + FLOWCAUSAL_DEFAULT_PACKAGE_RESCUE_NS;
			if (FLOWCAUSAL_DEFAULT_PACKAGE_TAIL_LOCALITY_AFTER_PY_ROOT &&
			    task->anchor_cpu == 4)
				package_tail_locality_until_ns =
					rescue_now + FLOWCAUSAL_DEFAULT_PACKAGE_TAIL_LOCALITY_NS;
		}
			if ((FLOWCAUSAL_DEFAULT_PACKAGE_FINAL_TAIL_AFTER_CXX_PY_ROOT ||
			     FLOWCAUSAL_DEFAULT_PACKAGE_FINAL_TAIL_FALLBACK_PERCPU) &&
			    (task->flags & FLOWCAUSAL_TASK_ROOT)) {
				__u64 final_now = bpf_ktime_get_ns();

			if (task->anchor_cpu == 0)
				cxx_root_exited = true;
			if (task->anchor_cpu == 4)
				py_root_exited = true;
			if (cxx_root_exited && py_root_exited)
					package_final_tail_until_ns =
						final_now + FLOWCAUSAL_DEFAULT_PACKAGE_FINAL_TAIL_NS;
			}
			if ((FLOWCAUSAL_DEFAULT_PACKAGE_FINAL_TAIL_AFTER_CRITICAL_CHILDREN ||
			     FLOWCAUSAL_DEFAULT_PARENT_EXIT_CLEANUP_PACKAGE_AFTER_CRITICAL ||
			     FLOWCAUSAL_DEFAULT_PARENT_EXIT_CLEANUP_SKIP_WHILE_PY_CRITICAL_ACTIVE ||
			     FLOWCAUSAL_DEFAULT_PY_TEST_RUNTIME_TAIL_AFTER_PACKAGE_CRITICAL) &&
			    !(task->flags & FLOWCAUSAL_TASK_ROOT)) {
				__u64 final_now = bpf_ktime_get_ns();

				if (task->anchor_cpu == 0 &&
				    (task->flags & FLOWCAUSAL_TASK_ROOT_DIRECT_SECOND))
					cxx_critical_child_exited = true;
				if (task->anchor_cpu == 4 &&
				    (task->flags & FLOWCAUSAL_TASK_ROOT_DIRECT_THIRD))
					py_critical_child_exited = true;
				if (task->anchor_cpu == 5 &&
				    (task->flags & FLOWCAUSAL_TASK_ROOT_DIRECT_THIRD))
					package_critical_child_exited = true;
				if (task->anchor_cpu == 5 &&
				    (task->flags & FLOWCAUSAL_TASK_ROOT_DIRECT_THIRD))
					mark_package_critical_done(task->session_id);
				if (FLOWCAUSAL_DEFAULT_PACKAGE_FINAL_TAIL_AFTER_CRITICAL_CHILDREN &&
				    cxx_critical_child_exited &&
				    py_critical_child_exited &&
				    package_critical_child_exited)
					package_final_tail_until_ns =
						final_now + FLOWCAUSAL_DEFAULT_PACKAGE_FINAL_TAIL_NS;
			}
			if (FLOWCAUSAL_DEFAULT_PACKAGE_TAIL_LOCALITY_AFTER_PY_TEST &&
			    task->anchor_cpu == 4 &&
			    (task->flags & FLOWCAUSAL_TASK_STAGE_TEST_TAIL) &&
		    task_is_direct_child_of_root_anchor(task, 4))
			package_tail_locality_until_ns =
				bpf_ktime_get_ns() + FLOWCAUSAL_DEFAULT_PACKAGE_TAIL_LOCALITY_NS;
		if (FLOWCAUSAL_DEFAULT_PACKAGE_RESCUE_WAIT_PY_TEST &&
		    task->anchor_cpu == 4 &&
		    (task->flags & FLOWCAUSAL_TASK_STAGE_TEST_TAIL) &&
		    task_is_direct_child_of_root_anchor(task, 4)) {
			if (FLOWCAUSAL_DEFAULT_PY_TEST_PROTECT_GRACE_NS)
				py_test_protect_until_ns =
					bpf_ktime_get_ns() +
					FLOWCAUSAL_DEFAULT_PY_TEST_PROTECT_GRACE_NS;
			else
				py_test_protect_until_ns = 0;
		}
		if ((FLOWCAUSAL_DEFAULT_PARENT_EXIT_BOOST ||
		     FLOWCAUSAL_DEFAULT_PARENT_EXIT_LATENCY ||
		     FLOWCAUSAL_DEFAULT_PARENT_EXIT_CLEANUP) &&
		    parent_exit_cleanup_admits(task) &&
		    task->parent_pid) {
			struct flow_task_key parent_key = { .pid = task->parent_pid };
			struct flow_task_ctx *parent;
			struct flow_task_ctx parent_ctx = {};
			struct flow_session_policy parent_policy = {};
				__u64 parent_session_id = FLOWCAUSAL_PARENT_EXIT_SESSION_ID;
				__u64 now = bpf_ktime_get_ns();
				__u32 cleanup_pulse_limit =
					parent_exit_cleanup_pulse_limit_for_task(task);
				__u64 cleanup_window_ns =
					parent_exit_cleanup_window_ns_for_task(task);

				parent = bpf_map_lookup_elem(&task_ctx, &parent_key);
				if ((FLOWCAUSAL_DEFAULT_PARENT_EXIT_LATENCY ||
				     FLOWCAUSAL_DEFAULT_PARENT_EXIT_CLEANUP) && parent &&
			    parent->session_id == FLOWCAUSAL_PARENT_EXIT_SESSION_ID) {
				parent->flags &= ~(FLOWCAUSAL_TASK_FRONTIER |
						   FLOWCAUSAL_TASK_COHORT_TAIL |
						   FLOWCAUSAL_TASK_PROPAGATE |
						   FLOWCAUSAL_TASK_BACKGROUND |
						   FLOWCAUSAL_TASK_OBSERVE_ONLY |
						   FLOWCAUSAL_TASK_MODE_MASK);
				parent->flags |= FLOWCAUSAL_TASK_PARENT_LATENCY |
						 FLOWCAUSAL_TASK_MODE_INTERACTIVE;
				if (FLOWCAUSAL_DEFAULT_PACKAGE_BG_PARENT_CLEANUP_SLICE_NS &&
				    task->anchor_cpu == 5 &&
				    (task->flags & FLOWCAUSAL_TASK_STAGE_PACKAGE_BG))
					parent->flags |= FLOWCAUSAL_TASK_STAGE_PACKAGE_BG;
				parent->confidence = 90;
				parent->last_update_ns = now;
				if (FLOWCAUSAL_DEFAULT_PACKAGE_BG_PARENT_CLEANUP_CPU >= 0 &&
				    task->anchor_cpu == 5 &&
				    (task->flags & FLOWCAUSAL_TASK_STAGE_PACKAGE_BG))
					parent->anchor_cpu =
						FLOWCAUSAL_DEFAULT_PACKAGE_BG_PARENT_CLEANUP_CPU;
				else if (FLOWCAUSAL_DEFAULT_PARENT_EXIT_CLEANUP_CHILD_CPU)
					parent->anchor_cpu = task->anchor_cpu;
				parent->propagation_depth = cleanup_pulse_limit;
				if (FLOWCAUSAL_DEFAULT_PARENT_EXIT_CLEANUP)
					parent->stage_id = now + cleanup_window_ns;
				} else if (FLOWCAUSAL_DEFAULT_PARENT_EXIT_BOOST && parent &&
					   parent->session_id == FLOWCAUSAL_PARENT_EXIT_SESSION_ID) {
				parent->flags |= FLOWCAUSAL_TASK_FRONTIER |
						 FLOWCAUSAL_TASK_COHORT_TAIL |
						 FLOWCAUSAL_TASK_BPF_AUTO;
				parent->flags &= ~(FLOWCAUSAL_TASK_BACKGROUND |
						   FLOWCAUSAL_TASK_OBSERVE_ONLY |
						   FLOWCAUSAL_TASK_MODE_MASK);
				parent->confidence = 90;
				parent->last_update_ns = now;
			} else if (!parent) {
				parent_ctx.session_id = FLOWCAUSAL_PARENT_EXIT_SESSION_ID;
				parent_ctx.stage_id = 0;
				parent_ctx.cohort_id = task->session_id;
				parent_ctx.last_update_ns = now;
				parent_ctx.tgid = task->parent_pid;
				parent_ctx.parent_pid = 0;
				if (FLOWCAUSAL_DEFAULT_PARENT_EXIT_LATENCY ||
				    FLOWCAUSAL_DEFAULT_PARENT_EXIT_CLEANUP)
					parent_ctx.flags =
						FLOWCAUSAL_TASK_PARENT_LATENCY |
						FLOWCAUSAL_TASK_MODE_INTERACTIVE;
				else
					parent_ctx.flags =
						FLOWCAUSAL_TASK_FRONTIER |
						FLOWCAUSAL_TASK_COHORT_TAIL |
						FLOWCAUSAL_TASK_BPF_AUTO;
				if (FLOWCAUSAL_DEFAULT_PACKAGE_BG_PARENT_CLEANUP_SLICE_NS &&
				    task->anchor_cpu == 5 &&
				    (task->flags & FLOWCAUSAL_TASK_STAGE_PACKAGE_BG))
					parent_ctx.flags |= FLOWCAUSAL_TASK_STAGE_PACKAGE_BG;
				parent_ctx.confidence = 90;
				if (FLOWCAUSAL_DEFAULT_PACKAGE_BG_PARENT_CLEANUP_CPU >= 0 &&
				    task->anchor_cpu == 5 &&
				    (task->flags & FLOWCAUSAL_TASK_STAGE_PACKAGE_BG))
					parent_ctx.anchor_cpu =
						FLOWCAUSAL_DEFAULT_PACKAGE_BG_PARENT_CLEANUP_CPU;
				else {
					parent_ctx.anchor_cpu =
						FLOWCAUSAL_DEFAULT_PARENT_EXIT_CLEANUP_CHILD_CPU ?
						task->anchor_cpu : -1;
				}
				parent_ctx.propagation_depth = cleanup_pulse_limit;
				if (FLOWCAUSAL_DEFAULT_PARENT_EXIT_CLEANUP)
					parent_ctx.stage_id =
						now + cleanup_window_ns;
				bpf_map_update_elem(&task_ctx, &parent_key,
						    &parent_ctx, BPF_ANY);
				}
				parent_exit_cleanup_mark_admitted(task);

				if (FLOWCAUSAL_DEFAULT_PARENT_EXIT_BOOST) {
				parent_policy.flags = FLOWCAUSAL_POLICY_ENABLE |
						      FLOWCAUSAL_POLICY_WIDTH_CTL;
				parent_policy.min_confidence = 75;
				parent_policy.max_frontier_width = 1;
				parent_policy.max_vtime_boost = default_max_vtime_boost;
				parent_policy.window_ns = default_window_ns;
				parent_policy.boost_budget_ns = default_window_ns / 10;
				parent_policy.expires_ns =
					now + FLOWCAUSAL_DEFAULT_PARENT_EXIT_BOOST_NS;
				parent_policy.anchor_cpu = -1;
				parent_policy.anchor_span = 1;
				bpf_map_update_elem(&session_policy, &parent_session_id,
						    &parent_policy, BPF_ANY);
			}
		}
	}
	return 0;
}

SEC("tp_btf/sched_process_free")
int BPF_PROG(flowcausal_on_free, struct task_struct *p)
{
	struct flow_task_key key = {};
	struct flow_task_ctx *task;

	if (!p)
		return 0;

	key.pid = p->pid;
	bpf_map_delete_elem(&shared_wait_start, &key);
	bpf_map_delete_elem(&root_child_wakeup_until, &key);
	bpf_map_delete_elem(&root_active_children, &key);
	bpf_map_delete_elem(&root_direct_child_seq, &key);
	task = bpf_map_lookup_elem(&task_ctx, &key);
	if (!task)
		return 0;

	/*
	 * sched_process_exit can fire before the final sched_ext stopping()
	 * callback has accounted runtime and released a width slot.  Defer map
	 * reclamation until sched_process_free, after the task has left normal
	 * scheduling paths, so short-lived propagated hints do not leak without
	 * truncating the last switch-out accounting.
	 */
	emit_event(FLOW_EVT_CLEANUP, p, task, task->flags);
	mark_session_dequeued(p, task);
	if (default_converge && task->session_id) {
		struct flow_session_runtime *sr;

		sr = bpf_map_lookup_elem(&session_runtime, &task->session_id);
		if (sr)
			__sync_fetch_and_add(&sr->exited_tasks, 1);
	}
	release_frontier_width(p);
	bpf_map_delete_elem(&task_ctx, &key);
	return 0;
}

static __always_inline int emit_wakeup_for_task(struct task_struct *p)
{
	struct flow_task_key key = {};
	struct flow_task_ctx *task;

	if (!p)
		return 0;

	key.pid = p->pid;
	task = bpf_map_lookup_elem(&task_ctx, &key);
	if (task)
		emit_event(FLOW_EVT_WAKEUP, p, task, task->flags);
	return 0;
}

SEC("tp_btf/sched_wakeup")
int BPF_PROG(flowcausal_on_wakeup, struct task_struct *p)
{
	return emit_wakeup_for_task(p);
}

SEC("tp_btf/sched_wakeup_new")
int BPF_PROG(flowcausal_on_wakeup_new, struct task_struct *p)
{
	return emit_wakeup_for_task(p);
}

SCX_OPS_DEFINE(flowcausal_ops,
	.select_cpu	= (void *)flowcausal_select_cpu,
	.enqueue	= (void *)flowcausal_enqueue,
	.dispatch	= (void *)flowcausal_dispatch,
	.running	= (void *)flowcausal_running,
	.stopping	= (void *)flowcausal_stopping,
	.enable		= (void *)flowcausal_enable,
	.init		= (void *)flowcausal_init,
	.exit		= (void *)flowcausal_exit,
	.name		= FLOWCAUSAL_OPS_NAME);

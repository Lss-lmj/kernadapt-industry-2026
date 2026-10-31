/* SPDX-License-Identifier: GPL-2.0 */
/* LockBoost ablation: shared LockCoop observation, identity, and state paths. */
#define LOCKCOOP_OPS lockboost_ops
#define LOCKCOOP_SCHED_NAME "lockboost"
#define LOCKCOOP_DEFAULT_NEST_ENABLED false
#define LOCKCOOP_DEFAULT_BOOST_ENABLED true
#define LOCKCOOP_DEFAULT_BOOST_PROFILE BOOST_PROFILE_BOUNDED
#include "scx_lockcoop.bpf.c"

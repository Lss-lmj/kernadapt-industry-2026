/* SPDX-License-Identifier: LGPL-2.1-or-later */
#ifndef __LOCKCOOP_USER_SYNC_H
#define __LOCKCOOP_USER_SYNC_H

#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

/* Call wait once before entering the contended retry loop. owner_tid may be 0. */
void lockcoop_spin_wait(const void *object, uint32_t owner_tid);
void lockcoop_spin_acquired(const void *object);
void lockcoop_spin_release(const void *object);

/* CAS annotations cover owner-bearing CAS loops and queue handoff points. */
void lockcoop_cas_wait(const void *object, uint32_t owner_tid);
void lockcoop_cas_acquired(const void *object);
void lockcoop_cas_release(const void *object);

#ifdef __cplusplus
}
#endif

#endif /* __LOCKCOOP_USER_SYNC_H */

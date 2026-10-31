USER_APP = handoff_locality_monitor
BPF_PROG_NAME = handoff_locality
USER_SRCS = src/userspace/handoff_locality_monitor.c
USER_OBJS = $(patsubst src/userspace/%.c,build/%.o,$(USER_SRCS))

CLANG ?= clang
BPFTOOL ?= bpftool

BPF_CFLAGS = -g -O2 -target bpf -I./src/bpf -I../../lock-monitorV3/src/bpf -I/usr/include
USER_CFLAGS = -g -O2 -Wall -I./src/userspace -I./src/bpf -I./build
USER_LDFLAGS = -lbpf -lelf -lz

.PHONY: all clean

all: build_dir $(USER_APP)

build_dir:
	@mkdir -p build

$(USER_APP): $(USER_OBJS) build/$(BPF_PROG_NAME).skel.h
	$(CLANG) $(USER_CFLAGS) -o $@ $(USER_OBJS) $(USER_LDFLAGS)

$(USER_OBJS): build/$(BPF_PROG_NAME).skel.h

build/%.o: src/userspace/%.c
	$(CLANG) $(USER_CFLAGS) -c $< -o $@

build/%.skel.h: build/%.bpf.o
	$(BPFTOOL) gen skeleton $< > $@

build/%.bpf.o: src/bpf/$(BPF_PROG_NAME).bpf.c
	$(CLANG) $(BPF_CFLAGS) -c $< -o $@

clean:
	rm -rf build $(USER_APP)

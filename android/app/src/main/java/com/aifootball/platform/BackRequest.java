package com.aifootball.platform;

/** One asynchronous dialog check per back gesture; stale callbacks cannot navigate. */
final class BackRequest {
    private long generation;
    private long pending;

    long begin() {
        if (pending != 0) return 0;
        pending = ++generation;
        return pending;
    }

    boolean complete(long token) {
        if (token == 0 || token != pending) return false;
        pending = 0;
        return true;
    }

    void cancel() { pending = 0; generation++; }
}

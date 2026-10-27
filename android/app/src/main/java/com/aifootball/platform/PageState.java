package com.aifootball.platform;

/** A late onPageFinished callback must never hide a main-frame failure. */
public final class PageState {
    private boolean failed;
    private String document;
    public void started(String url) { document = url; failed = false; }
    public void failed() { failed = true; }
    public boolean canFinish(String url) { return !failed && document != null && document.equals(url); }
    public boolean hasFailed() { return failed; }
}

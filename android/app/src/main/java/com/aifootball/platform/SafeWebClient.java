package com.aifootball.platform;

import android.graphics.Bitmap;
import android.net.http.SslError;
import android.webkit.SslErrorHandler;
import android.webkit.WebResourceError;
import android.webkit.WebResourceRequest;
import android.webkit.WebResourceResponse;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import java.io.ByteArrayInputStream;

final class SafeWebClient extends WebViewClient {
    interface Listener {
        void loading(boolean loading);
        void failed(String message);
        void external(String url);
        void rendererLost();
    }
    private final String service;
    private final PageState state;
    private final Listener listener;
    SafeWebClient(String service, PageState state, Listener listener) {
        this.service = service; this.state = state; this.listener = listener;
    }
    @Override public boolean shouldOverrideUrlLoading(WebView view, WebResourceRequest request) {
        UrlPolicy.Navigation target = UrlPolicy.classify(service, request.getUrl().toString());
        if (target == UrlPolicy.Navigation.INTERNAL) return false;
        if (target == UrlPolicy.Navigation.EXTERNAL && request.isForMainFrame() && request.hasGesture()) {
            listener.external(request.getUrl().toString());
        }
        return true;
    }
    @Override public WebResourceResponse shouldInterceptRequest(WebView view, WebResourceRequest request) {
        if (UrlPolicy.classify(service, request.getUrl().toString()) == UrlPolicy.Navigation.BLOCKED) {
            return new WebResourceResponse("text/plain", "UTF-8", new ByteArrayInputStream(new byte[0]));
        }
        return null;
    }
    @Override public void onPageStarted(WebView view, String url, Bitmap icon) {
        if (UrlPolicy.classify(service, url) != UrlPolicy.Navigation.INTERNAL) {
            view.stopLoading(); fail("已阻止离开配对服务的页面跳转"); return;
        }
        state.started(url); listener.loading(true);
    }
    @Override public void onPageFinished(WebView view, String url) {
        if (state.canFinish(url)) listener.loading(false);
    }
    @Override public void onReceivedError(WebView view, WebResourceRequest request, WebResourceError error) {
        if (request.isForMainFrame()) fail("暂时无法连接服务。请检查网络后重试。");
    }
    @Override public void onReceivedHttpError(WebView view, WebResourceRequest request, WebResourceResponse response) {
        if (request.isForMainFrame() && response.getStatusCode() >= 400) {
            fail("服务返回错误（" + response.getStatusCode() + "），请稍后重试。");
        }
    }
    @Override public void onReceivedSslError(WebView view, SslErrorHandler handler, SslError error) {
        handler.cancel(); fail("服务的安全证书无法验证。请联系管理员检查证书，修复后重试。");
    }
    @Override public boolean onRenderProcessGone(WebView view, android.webkit.RenderProcessGoneDetail detail) {
        fail("页面进程已结束，请关闭后重新打开应用。");
        listener.rendererLost();
        return true;
    }
    private void fail(String message) { state.failed(); listener.failed(message); }
}

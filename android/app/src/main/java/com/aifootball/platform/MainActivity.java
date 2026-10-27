package com.aifootball.platform;

import android.annotation.SuppressLint;
import android.app.Activity;
import android.app.AlertDialog;
import android.content.ActivityNotFoundException;
import android.content.Intent;
import android.graphics.Insets;
import android.net.ConnectivityManager;
import android.net.Network;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.view.View;
import android.view.WindowInsets;
import android.view.inputmethod.InputMethodManager;
import android.webkit.CookieManager;
import android.webkit.WebSettings;
import android.webkit.WebStorage;
import android.webkit.WebView;
import android.widget.Button;
import android.widget.EditText;
import android.widget.ProgressBar;
import android.widget.TextView;
import android.widget.Toast;
import java.security.GeneralSecurityException;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.Future;
import javax.net.ssl.SSLException;

public final class MainActivity extends Activity {
    private WebView webView;
    private View pairing, offline;
    private TextView hostLabel, offlineMessage, pairingMessage;
    private EditText serviceInput;
    private Button verifyButton, returnButton;
    private ProgressBar progress;
    private PairingStore pairingStore;
    private String service;
    private boolean rendererLost;
    private final PageState page = new PageState();
    private final ExecutorService executor = Executors.newSingleThreadExecutor();
    private Future<?> probe;
    private int probeGeneration;
    private ConnectivityManager connectivity;
    private ConnectivityManager.NetworkCallback networkCallback;

    @SuppressLint("SetJavaScriptEnabled")
    @Override public void onCreate(Bundle state) {
        super.onCreate(state);
        try { setContentView(R.layout.activity_main); }
        catch (RuntimeException componentUnavailable) {
            android.widget.LinearLayout error = new android.widget.LinearLayout(this);
            error.setOrientation(android.widget.LinearLayout.VERTICAL); error.setGravity(android.view.Gravity.CENTER);
            int padding = (int) (24 * getResources().getDisplayMetrics().density); error.setPadding(padding,padding,padding,padding);
            TextView explanation = new TextView(this); explanation.setText(R.string.web_component_unavailable); explanation.setTextSize(18);
            Button close = new Button(this); close.setText(R.string.close_app); close.setOnClickListener(view -> finish());
            error.addView(explanation); error.addView(close); setContentView(error);
            return;
        }
        webView = findViewById(R.id.platform_webview);
        offline = findViewById(R.id.offline_panel);
        offlineMessage = findViewById(R.id.offline_message);
        pairing = findViewById(R.id.pairing_panel);
        hostLabel = findViewById(R.id.service_label);
        pairingMessage = findViewById(R.id.pairing_message);
        serviceInput = findViewById(R.id.service_input);
        verifyButton = findViewById(R.id.verify_service);
        returnButton = findViewById(R.id.return_service);
        progress = findViewById(R.id.load_progress);
        if (Build.VERSION.SDK_INT >= 28) findViewById(R.id.pairing_heading).setAccessibilityHeading(true);
        if (Build.VERSION.SDK_INT >= 33) getOnBackInvokedDispatcher().registerOnBackInvokedCallback(
                android.window.OnBackInvokedDispatcher.PRIORITY_DEFAULT, this::handleBack);
        pairingStore = new PairingStore(this);
        configureWebView();
        findViewById(R.id.client_menu).setOnClickListener(v -> showMenu());
        findViewById(R.id.retry_page).setOnClickListener(v -> openService());
        findViewById(R.id.change_service).setOnClickListener(v -> showPairing());
        verifyButton.setOnClickListener(v -> verifyService());
        returnButton.setOnClickListener(v -> { cancelProbe(); openService(); });
        applyInsets();
        registerNetworkState();
        try {
            service = pairingStore.read();
            if (service == null) showPairing(); else openService();
        } catch (GeneralSecurityException e) {
            pairingStore.clear();
            showPairing(); pairingMessage.setText(R.string.pairing_reset);
        }
    }

    @SuppressLint("SetJavaScriptEnabled")
    private void configureWebView() {
        WebSettings settings = webView.getSettings();
        settings.setJavaScriptEnabled(true);
        settings.setDomStorageEnabled(true);
        settings.setBuiltInZoomControls(false);
        settings.setDisplayZoomControls(false);
        settings.setAllowFileAccess(false);
        settings.setAllowContentAccess(false);
        settings.setMixedContentMode(WebSettings.MIXED_CONTENT_NEVER_ALLOW);
        settings.setGeolocationEnabled(false);
        settings.setJavaScriptCanOpenWindowsAutomatically(false);
        settings.setSupportMultipleWindows(false);
        settings.setSafeBrowsingEnabled(true);
        CookieManager.getInstance().setAcceptThirdPartyCookies(webView, false);
        WebView.setWebContentsDebuggingEnabled(BuildConfig.DEBUG);
        webView.setDownloadListener((url, agent, disposition, type, size) ->
                Toast.makeText(this, R.string.download_unavailable, Toast.LENGTH_SHORT).show());
    }

    private void applyInsets() {
        findViewById(R.id.client_root).setOnApplyWindowInsetsListener((view, insets) -> {
            if (Build.VERSION.SDK_INT >= 30) {
                Insets bars = insets.getInsets(WindowInsets.Type.systemBars() | WindowInsets.Type.displayCutout());
                view.setPadding(bars.left, bars.top, bars.right, bars.bottom);
            } else {
                view.setPadding(insets.getSystemWindowInsetLeft(), insets.getSystemWindowInsetTop(),
                        insets.getSystemWindowInsetRight(), insets.getSystemWindowInsetBottom());
            }
            return insets;
        });
    }

    private void showPairing() {
        cancelProbe();
        if (rendererLost) { recreate(); return; }
        webView.stopLoading(); progress.setVisibility(View.GONE); offline.setVisibility(View.GONE);
        pairing.setVisibility(View.VISIBLE); webView.setVisibility(View.GONE);
        serviceInput.setText(service == null ? "" : service);
        returnButton.setVisibility(service == null ? View.GONE : View.VISIBLE);
        hostLabel.setText(R.string.pairing_title);
        pairingMessage.setText(R.string.pairing_help);
    }

    private void verifyService() {
        final String candidate;
        try { candidate = UrlPolicy.normalizeService(serviceInput.getText().toString()); }
        catch (IllegalArgumentException e) { serviceInput.setError(e.getMessage()); return; }
        cancelProbe();
        int generation = probeGeneration;
        verifyButton.setEnabled(false); serviceInput.setEnabled(false);
        pairingMessage.setText(R.string.verifying_service);
        ((InputMethodManager) getSystemService(INPUT_METHOD_SERVICE)).hideSoftInputFromWindow(serviceInput.getWindowToken(), 0);
        probe = executor.submit(() -> {
            try {
                ServerProbe.Identity identity = new ServerProbe().verify(candidate, BuildConfig.VERSION_CODE);
                runOnUiThread(() -> {
                    if (isFinishing() || isDestroyed() || generation != probeGeneration) return;
                    finishProbe(); confirmService(candidate, identity.version);
                });
            } catch (Exception e) {
                String reason = e instanceof SSLException ? getString(R.string.certificate_error)
                        : e instanceof java.net.SocketTimeoutException ? getString(R.string.probe_timeout)
                        : e.getMessage();
                runOnUiThread(() -> {
                    if (isFinishing() || isDestroyed() || generation != probeGeneration) return;
                    finishProbe(); pairingMessage.setText(reason == null ? getString(R.string.probe_failed) : reason);
                });
            }
        });
    }

    private void confirmService(String candidate, String version) {
        new AlertDialog.Builder(this).setTitle(R.string.confirm_service)
                .setMessage(getString(R.string.confirm_service_description, candidate, version))
                .setNegativeButton(R.string.cancel, null).setPositiveButton(R.string.pair_service, (dialog, which) -> {
                    try {
                        pairingStore.write(candidate);
                        clearWebIdentity(() -> { service = candidate; openService(); });
                    } catch (GeneralSecurityException e) { pairingMessage.setText(R.string.pairing_save_failed); }
                }).show();
    }

    private void finishProbe() { verifyButton.setEnabled(true); serviceInput.setEnabled(true); }
    private void cancelProbe() { probeGeneration++; if (probe != null) probe.cancel(true); finishProbe(); }

    private void openService() {
        cancelProbe();
        if (rendererLost) { recreate(); return; }
        if (service == null) { showPairing(); return; }
        pairing.setVisibility(View.GONE); offline.setVisibility(View.GONE); webView.setVisibility(View.VISIBLE);
        hostLabel.setText(Uri.parse(service).getAuthority());
        webView.setWebViewClient(new SafeWebClient(service, page, new SafeWebClient.Listener() {
            @Override public void loading(boolean loading) { progress.setVisibility(loading ? View.VISIBLE : View.GONE); }
            @Override public void failed(String message) {
                progress.setVisibility(View.GONE); offlineMessage.setText(message); offline.setVisibility(View.VISIBLE);
            }
            @Override public void external(String url) { confirmExternal(url); }
            @Override public void rendererLost() {
                rendererLost = true;
                ((android.view.ViewGroup) webView.getParent()).removeView(webView);
                webView.destroy();
            }
        }));
        webView.loadUrl(service + "/");
    }

    private void confirmExternal(String url) {
        new AlertDialog.Builder(this).setTitle(R.string.external_link)
                .setMessage(getString(R.string.external_link_description, Uri.parse(url).getAuthority()))
                .setNegativeButton(R.string.cancel, null).setPositiveButton(R.string.open_browser, (dialog, which) -> {
                    try { startActivity(new Intent(Intent.ACTION_VIEW, Uri.parse(url)).addCategory(Intent.CATEGORY_BROWSABLE)); }
                    catch (ActivityNotFoundException e) { Toast.makeText(this, R.string.browser_unavailable, Toast.LENGTH_LONG).show(); }
                }).show();
    }

    private void showMenu() {
        new AlertDialog.Builder(this).setTitle(getString(R.string.app_version, BuildConfig.VERSION_NAME))
                .setItems(R.array.client_menu, (dialog, which) -> {
                    if (which == 0) { if (service != null) openService(); }
                    else if (which == 1) showPairing();
                    else if (which == 2) confirmClearIdentity();
                }).show();
    }

    private void confirmClearIdentity() {
        new AlertDialog.Builder(this).setTitle(R.string.clear_login)
                .setMessage(R.string.clear_login_description).setNegativeButton(R.string.cancel, null)
                .setPositiveButton(R.string.clear_login, (dialog, which) -> clearWebIdentity(this::openService)).show();
    }

    private void clearWebIdentity(Runnable after) {
        if (!rendererLost) { webView.stopLoading(); webView.clearCache(true); webView.clearHistory(); }
        WebStorage.getInstance().deleteAllData();
        CookieManager.getInstance().removeAllCookies(removed -> {
            CookieManager.getInstance().flush();
            if (!isFinishing() && !isDestroyed()) { if (rendererLost) recreate(); else after.run(); }
        });
    }

    private void registerNetworkState() {
        connectivity = (ConnectivityManager) getSystemService(CONNECTIVITY_SERVICE);
        networkCallback = new ConnectivityManager.NetworkCallback() {
            @Override public void onAvailable(Network network) {
                runOnUiThread(() -> {
                    if (page.hasFailed() && !isFinishing()) offlineMessage.setText(R.string.network_restored);
                });
            }
        };
        connectivity.registerDefaultNetworkCallback(networkCallback);
    }

    @Override public void onBackPressed() { handleBack(); }
    private void handleBack() {
        if (pairing.getVisibility() == View.VISIBLE && service != null) { cancelProbe(); openService(); }
        else if (!rendererLost && offline.getVisibility() != View.VISIBLE && webView.canGoBack()) webView.goBack();
        else finish();
    }
    @Override protected void onPause() { if (webView != null && !rendererLost) { CookieManager.getInstance().flush(); webView.onPause(); } super.onPause(); }
    @Override protected void onResume() { super.onResume(); if (webView != null && !rendererLost) webView.onResume(); }
    @Override protected void onDestroy() {
        probeGeneration++; if (probe != null) probe.cancel(true); executor.shutdownNow();
        if (connectivity != null && networkCallback != null) connectivity.unregisterNetworkCallback(networkCallback);
        if (webView != null && !rendererLost) { webView.stopLoading(); webView.destroy(); }
        super.onDestroy();
    }
}

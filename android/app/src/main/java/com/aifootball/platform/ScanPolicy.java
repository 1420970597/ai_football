package com.aifootball.platform;

/** Scanner output is untrusted input; never a saved pairing or login. */
final class ScanPolicy {
    static final String ACTION = "com.google.zxing.client.android.SCAN";
    static final String[] PACKAGES = {"com.google.zxing.client.android", "com.srowen.bs.android"};
    static final int MAX_RESULT_CHARS = 2048;
    private ScanPolicy() {}

    static String candidate(String result, String format) {
        if (!"QR_CODE".equals(format) || result == null || result.length() > MAX_RESULT_CHARS) {
            throw new IllegalArgumentException("二维码内容不是有效的服务地址");
        }
        return UrlPolicy.normalizeService(result);
    }
}

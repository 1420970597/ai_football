package com.aifootball.platform;

import java.net.URI;
import java.net.URISyntaxException;
import java.util.Locale;

/** No Android dependencies: the same policy is exercised by JVM tests. */
public final class UrlPolicy {
    public enum Navigation { INTERNAL, EXTERNAL, BLOCKED }
    private UrlPolicy() {}

    public static String normalizeService(String input) {
        if (input == null) throw new IllegalArgumentException("请输入服务地址");
        String value = input.trim();
        URI uri = secureUri(value);
        if (uri.getRawQuery() != null || uri.getRawFragment() != null
                || !(uri.getRawPath().isEmpty() || uri.getRawPath().equals("/"))) {
            throw new IllegalArgumentException("服务地址只填写 HTTPS 域名和端口，不包含页面路径");
        }
        return origin(uri);
    }

    public static Navigation classify(String service, String target) {
        try {
            URI uri = secureUri(target);
            return origin(uri).equals(normalizeService(service)) ? Navigation.INTERNAL : Navigation.EXTERNAL;
        } catch (IllegalArgumentException ignored) { return Navigation.BLOCKED; }
    }

    private static URI secureUri(String value) {
        if (value == null || value.length() > 8192) throw new IllegalArgumentException("地址不合法");
        for (int i = 0; i < value.length(); i++) {
            char ch = value.charAt(i);
            if (Character.isWhitespace(ch) || Character.isISOControl(ch) || ch == '\\') {
                throw new IllegalArgumentException("地址包含不安全字符");
            }
        }
        try {
            URI uri = new URI(value);
            if (!"https".equalsIgnoreCase(uri.getScheme()) || uri.isOpaque()
                    || uri.getHost() == null || uri.getRawUserInfo() != null
                    || uri.getRawAuthority().contains("%") || uri.getHost().endsWith(".")
                    || uri.getPort() == 0 || uri.getPort() > 65535 || uri.getRawAuthority().endsWith(":")) {
                throw new IllegalArgumentException("请使用有效的 HTTPS 服务地址，不能包含账号或密码");
            }
            return uri;
        } catch (URISyntaxException e) { throw new IllegalArgumentException("服务地址格式错误"); }
    }

    private static String origin(URI uri) {
        String host = uri.getHost().toLowerCase(Locale.ROOT);
        return "https://" + host + ((uri.getPort() == -1 || uri.getPort() == 443) ? "" : ":" + uri.getPort());
    }
}

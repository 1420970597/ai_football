package com.aifootball.platform;

import org.json.JSONException;
import org.json.JSONObject;
import java.io.ByteArrayOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.net.URL;
import java.nio.charset.StandardCharsets;
import javax.net.ssl.HttpsURLConnection;

final class ServerProbe {
    static final int MAX_BYTES = 16384;
    static final class Identity {
        final String version;
        Identity(String version) { this.version = version; }
    }

    Identity verify(String origin, int clientVersion) throws IOException {
        String normalized = UrlPolicy.normalizeService(origin);
        HttpsURLConnection connection = (HttpsURLConnection) new URL(normalized + "/api/v1/public/client-info").openConnection();
        connection.setInstanceFollowRedirects(false);
        connection.setConnectTimeout(5000);
        connection.setReadTimeout(5000);
        connection.setRequestProperty("Accept", "application/json");
        connection.setRequestProperty("Accept-Encoding", "identity");
        connection.setRequestProperty("User-Agent", "BallNewsAndroid/" + clientVersion);
        connection.setUseCaches(false);
        long deadline = System.nanoTime() + 15_000_000_000L;
        try {
            if (connection.getResponseCode() != 200) throw new IOException("服务验证失败，请检查地址及网关状态");
            String type = connection.getContentType();
            if (type == null || !type.toLowerCase(java.util.Locale.ROOT).startsWith("application/json")) {
                throw new IOException("该地址未返回球讯服务协议");
            }
            if (connection.getContentLengthLong() > MAX_BYTES) throw new IOException("服务信息响应过大");
            try (InputStream stream = connection.getInputStream(); ByteArrayOutputStream output = new ByteArrayOutputStream()) {
                byte[] buffer = new byte[1024];
                int count;
                while ((count = stream.read(buffer)) != -1) {
                    if (Thread.currentThread().isInterrupted() || System.nanoTime() > deadline) throw new IOException("服务验证超时");
                    if (output.size() + count > MAX_BYTES) throw new IOException("服务信息响应过大");
                    output.write(buffer, 0, count);
                }
                return parse(output.toString(StandardCharsets.UTF_8.name()), clientVersion);
            }
        } finally { connection.disconnect(); }
    }

    static Identity parse(String body, int clientVersion) throws IOException {
        if (body.getBytes(StandardCharsets.UTF_8).length > MAX_BYTES) throw new IOException("服务信息响应过大");
        try {
            JSONObject info = new JSONObject(body);
            if (!"ai-football-experts".equals(info.getString("product_id")) || integer(info, "api_version") != 2) {
                throw new IOException("服务协议不兼容，请检查地址");
            }
            if (integer(info, "min_android_version_code") > clientVersion) throw new IOException("当前客户端版本过低，请升级后配对");
            String version = info.getString("version");
            if (version.length() > 80 || !version.matches("[A-Za-z0-9.+_-]{1,80}")) throw new IOException("服务版本信息无效");
            return new Identity(version);
        } catch (JSONException | IllegalArgumentException e) { throw new IOException("服务信息格式不正确", e); }
    }

    private static int integer(JSONObject info, String name) throws JSONException, IOException {
        Object raw = info.get(name);
        if (!(raw instanceof Integer || raw instanceof Long) || ((Number) raw).longValue() < 0
                || ((Number) raw).longValue() > Integer.MAX_VALUE) throw new IOException("服务版本信息无效");
        return ((Number) raw).intValue();
    }
}

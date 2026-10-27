package com.aifootball.platform.fixture;

import java.io.BufferedReader;
import java.io.InputStream;
import java.io.InputStreamReader;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.security.KeyStore;
import javax.net.ssl.KeyManagerFactory;
import javax.net.ssl.SSLContext;
import javax.net.ssl.SSLServerSocket;
import javax.net.ssl.SSLSocket;

/** Isolated device fixture. Never points to upstream accounts or real recommendations. */
public final class ServerFixture {
    private static final String INFO="{\"product_id\":\"ai-football-experts\",\"api_version\":2,\"min_android_version_code\":1,\"version\":\"2.0.0-device-fixture\"}";
    public static void main(String[] args) throws Exception {
        KeyStore keys=KeyStore.getInstance("PKCS12");
        try(InputStream stream=Files.newInputStream(Path.of(args[0]))) { keys.load(stream,"fixture-only".toCharArray()); }
        KeyManagerFactory manager=KeyManagerFactory.getInstance(KeyManagerFactory.getDefaultAlgorithm()); manager.init(keys,"fixture-only".toCharArray());
        SSLContext tls=SSLContext.getInstance("TLS"); tls.init(manager.getKeyManagers(),null,null);
        try(SSLServerSocket server=(SSLServerSocket)tls.getServerSocketFactory().createServerSocket(9443)) {
            System.out.println("fixture-listening:9443");
            while(true) {
                try(SSLSocket socket=(SSLSocket)server.accept()) { respond(socket); }
                catch(java.io.IOException expectedClientCancellation) { System.out.println("fixture-client-disconnected"); }
            }
        }
    }
    private static void respond(SSLSocket socket) throws Exception {
        socket.setSoTimeout(8000);
        BufferedReader reader=new BufferedReader(new InputStreamReader(socket.getInputStream(),StandardCharsets.UTF_8));
        String first=reader.readLine(); if(first==null) return;
        String[] request=first.split(" "); String method=request[0],path=request[1]; boolean authenticated=false;
        String line; int length=0;
        while((line=reader.readLine())!=null && !line.isEmpty()) {
            if(line.startsWith("Cookie:") && line.contains("fixture_session=active")) authenticated=true;
            if(line.toLowerCase(java.util.Locale.ROOT).startsWith("content-length:")) length=Integer.parseInt(line.substring(15).trim());
        }
        if(length>16384) return;
        for(int i=0;i<length;i++) if(reader.read()==-1) break;
        int status=200; String extra="",body,type="text/html; charset=utf-8";
        if(path.equals("/api/v1/public/client-info")) { body=INFO; type="application/json"; }
        else if(path.equals("/login") && method.equals("POST")) {
            body=""; status=303; extra="Location: /experts\r\nSet-Cookie: fixture_session=active; Secure; HttpOnly; SameSite=Lax; Path=/; Max-Age=3600\r\n";
        } else if(path.equals("/expire")) {
            body=""; status=303; extra="Location: /login\r\nSet-Cookie: fixture_session=; Secure; HttpOnly; Path=/; Max-Age=0\r\n";
        } else if(path.equals("/http-error")) { status=503; body="unavailable fixture"; }
        else if(path.equals("/experts") && !authenticated) { body=""; status=303; extra="Location: /login\r\n"; }
        else if(path.equals("/login")) {
            body=html("登录（隔离样本）","<form method='post' action='/login'><label>测试账号<input name='username' value='fixture-user'></label><button>登录样本</button></form>");
        } else if(path.equals("/experts")) {
            body=html("专家推荐（隔离样本）","<p>已登录 fixture-user · 模拟数据</p><p>样本专家：小球观察员；推荐仅用于客户端阅读验收。</p><a href='/details'>查看样本详情</a><p><a href='/expire'>模拟会话过期</a></p>");
        } else if(path.equals("/details")) { body=html("推荐详情（隔离样本）","<p>赛前样本 · 全场大小 · 小 2.5 · 等待结算</p>"); }
        else { body=html("比分首页（隔离样本）","<p>所有比赛及账号均为测试样本。</p><p><a href='/experts'>专家阅读</a></p><p><a href='/http-error'>模拟服务故障</a></p><p><a href='https://outside.example.invalid/'>外部网站</a></p><p><a href='file:///system/build.prop'>危险地址</a></p>"); }
        byte[] bytes=body.getBytes(StandardCharsets.UTF_8);
        String header="HTTP/1.1 "+status+" Fixture\r\nContent-Type: "+type+"\r\nContent-Length: "+bytes.length+"\r\nCache-Control: no-store\r\nConnection: close\r\n"+extra+"\r\n";
        socket.getOutputStream().write(header.getBytes(StandardCharsets.US_ASCII)); socket.getOutputStream().write(bytes); socket.getOutputStream().flush();
        System.out.println(method+" "+path+" status="+status+" authenticated="+authenticated);
    }
    private static String html(String title,String content) {
        return "<!doctype html><html lang='zh-CN'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><title>球讯隔离验收</title><style>body{font:17px sans-serif;padding:16px;line-height:1.6}h1{font-size:22px}button,input{font-size:18px;padding:8px;max-width:95%}a{display:inline-block;padding:6px}</style></head><body><h1>"+title+"</h1>"+content+"</body></html>";
    }
}

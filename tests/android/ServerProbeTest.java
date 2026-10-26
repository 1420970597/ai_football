package com.aifootball.platform;

import java.io.BufferedReader;
import java.io.InputStreamReader;
import java.io.IOException;
import java.io.InputStream;
import java.net.InetAddress;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.security.KeyStore;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.atomic.AtomicReference;
import javax.net.ssl.KeyManagerFactory;
import javax.net.ssl.SSLContext;
import javax.net.ssl.SSLException;
import javax.net.ssl.SSLSocketFactory;
import javax.net.ssl.HttpsURLConnection;
import javax.net.ssl.SSLServerSocket;
import javax.net.ssl.SSLSocket;
import javax.net.ssl.TrustManagerFactory;
import org.junit.After;
import org.junit.AfterClass;
import org.junit.Before;
import org.junit.BeforeClass;
import org.junit.Test;
import static org.junit.Assert.*;

/** Local TLS fixture only; no production URLs, passwords, accounts or trust bypass in the APK. */
public final class ServerProbeTest {
    private static final String IDENTITY="{\"product_id\":\"ai-football-experts\",\"api_version\":2,\"min_android_version_code\":1,\"version\":\"2.0.0-dev\"}";
    private static Path keyFile;
    private static KeyStore keys;
    private static SSLContext context;
    private SSLServerSocket server;
    private ExecutorService handlers;
    private SSLSocketFactory originalFactory;
    private String origin;
    private volatile int responseStatus;
    private volatile String responseType;
    private volatile String responseBody;
    private final AtomicReference<String> seenPath=new AtomicReference<>();
    private final AtomicReference<String> seenCookie=new AtomicReference<>();

    @BeforeClass public static void tlsFixture() throws Exception {
        keyFile=Files.createTempFile("ballnews-tls-fixture-", ".p12"); Files.delete(keyFile);
        Process process=new ProcessBuilder(System.getProperty("java.home")+"/bin/keytool", "-genkeypair", "-alias", "fixture", "-keyalg", "RSA", "-keysize", "2048",
            "-validity", "2", "-dname", "CN=localhost", "-ext", "SAN=dns:localhost", "-keystore", keyFile.toString(), "-storetype", "PKCS12", "-storepass", "fixture-only")
            .redirectErrorStream(true).start();
        byte[] diagnostics=process.getInputStream().readAllBytes();
        assertEquals(new String(diagnostics,StandardCharsets.UTF_8),0,process.waitFor());
        keys=KeyStore.getInstance("PKCS12");
        try (InputStream stream=Files.newInputStream(keyFile)) { keys.load(stream,"fixture-only".toCharArray()); }
        KeyManagerFactory managers=KeyManagerFactory.getInstance(KeyManagerFactory.getDefaultAlgorithm()); managers.init(keys,"fixture-only".toCharArray());
        TrustManagerFactory trust=TrustManagerFactory.getInstance(TrustManagerFactory.getDefaultAlgorithm()); trust.init(keys);
        context=SSLContext.getInstance("TLS"); context.init(managers.getKeyManagers(),trust.getTrustManagers(),null);
    }
    @AfterClass public static void deleteFixture() throws Exception { if(keyFile!=null) Files.deleteIfExists(keyFile); }
    @Before public void localServer() throws Exception {
        responseStatus=200; responseType="application/json; charset=utf-8"; responseBody=IDENTITY;
        originalFactory=HttpsURLConnection.getDefaultSSLSocketFactory();
        server=(SSLServerSocket)context.getServerSocketFactory().createServerSocket(0,10,InetAddress.getByName("127.0.0.1"));
        handlers=Executors.newFixedThreadPool(2);
        handlers.submit(() -> {
            while(!server.isClosed()) {
                try { respond((SSLSocket)server.accept()); }
                catch (IOException expectedConnectionOrCertificateFailure) { /* TLS rejection is an asserted case. */ }
            }
        });
        origin="https://localhost:"+server.getLocalPort();
    }
    private void respond(SSLSocket accepted) throws IOException {
        try (SSLSocket socket=accepted) {
            socket.setSoTimeout(5000);
            BufferedReader input=new BufferedReader(new InputStreamReader(socket.getInputStream(),StandardCharsets.US_ASCII));
            String line=input.readLine();
            if(line==null) return;
            seenPath.set(line.split(" ")[1]);
            while((line=input.readLine())!=null && !line.isEmpty()) {
                if(line.toLowerCase(java.util.Locale.ROOT).startsWith("cookie:")) seenCookie.set(line);
            }
            byte[] data=responseBody.getBytes(StandardCharsets.UTF_8);
            String header="HTTP/1.1 "+responseStatus+" Fixture\r\nContent-Type: "+responseType+"\r\nContent-Length: "+data.length
                +"\r\nLocation: https://not-contacted.example/\r\nConnection: close\r\n\r\n";
            socket.getOutputStream().write(header.getBytes(StandardCharsets.US_ASCII));
            socket.getOutputStream().write(data); socket.getOutputStream().flush();
        }
    }
    @After public void shutdown() throws Exception {
        HttpsURLConnection.setDefaultSSLSocketFactory(originalFactory);
        server.close(); handlers.shutdownNow();
    }
    private void trustOnlyFixtureForThisTest() { HttpsURLConnection.setDefaultSSLSocketFactory(context.getSocketFactory()); }
    @Test public void defaultTrustRejectsUnknownCertificate() {
        assertThrows(SSLException.class,() -> new ServerProbe().verify(origin,20000));
        assertNull(seenPath.get());
    }
    @Test public void verifiesExactPathWithoutWebViewCookies() throws Exception {
        trustOnlyFixtureForThisTest(); assertEquals("2.0.0-dev",new ServerProbe().verify(origin,20000).version);
        assertEquals("/api/v1/public/client-info",seenPath.get()); assertNull(seenCookie.get());
    }
    @Test public void redirectsAreRejected() {
        trustOnlyFixtureForThisTest(); responseStatus=302;
        assertThrows(IOException.class,() -> new ServerProbe().verify(origin,20000));
        assertEquals("/api/v1/public/client-info",seenPath.get());
    }
    @Test public void htmlCannotPairAsService() {
        trustOnlyFixtureForThisTest(); responseType="text/html"; responseBody="<html>proxy</html>";
        assertThrows(IOException.class,() -> new ServerProbe().verify(origin,20000));
    }
    @Test public void declaredOversizeResponseRejected() {
        trustOnlyFixtureForThisTest(); responseBody="x".repeat(ServerProbe.MAX_BYTES+1);
        assertThrows(IOException.class,() -> new ServerProbe().verify(origin,20000));
    }
    @Test public void serverFailureCannotPair() {
        trustOnlyFixtureForThisTest(); responseStatus=503;
        assertThrows(IOException.class,() -> new ServerProbe().verify(origin,20000));
    }
}

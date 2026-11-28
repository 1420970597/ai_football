package com.aifootball.platform;

import org.junit.Test;
import static org.junit.Assert.*;
import java.io.IOException;

public final class ClientPolicyTest {
    @Test public void canonicalHttpsOrigins() {
        assertEquals("https://scores.example", UrlPolicy.normalizeService(" HTTPS://Scores.Example:443/ "));
        assertEquals("https://scores.example:9443", UrlPolicy.normalizeService("https://scores.example:9443"));
        assertEquals("https://[2001:db8::1]:9443", UrlPolicy.normalizeService("https://[2001:db8::1]:9443/"));
    }
    @Test public void canonicalHttpOriginsWithoutCertificateRequirement() {
        assertEquals("http://scores.example", UrlPolicy.normalizeService(" HTTP://Scores.Example:80/ "));
        assertEquals("http://192.0.2.1:3003", UrlPolicy.normalizeService("http://192.0.2.1:3003/"));
        assertEquals("http://[2001:db8::1]", UrlPolicy.normalizeService("http://[2001:db8::1]:80/"));
        assertEquals("http://scores.example:443", UrlPolicy.normalizeService("http://scores.example:443"));
        assertEquals("https://scores.example:80", UrlPolicy.normalizeService("https://scores.example:80"));
    }
    @Test public void rejectMalformedOrCredentialBearingPairing() {
        String[] rejected = {"ftp://scores.example", "//scores.example", "javascript:alert(1)", "https://u:p@scores.example",
            "https://scores.example/user", "https://scores.example?token=abc", "https://scores.example#x",
            "https://scores.example.", "https://scores.example:", "https://scores.example:0", "https://scores.example:65536",
            "https://scores.example\\@evil.example", "https://scores.example\n.evil.example", "https://%73cores.example",
            "https://scores.example%2f.evil.example", "https://scores.example:bad", "http://u:p@scores.example",
            "http://scores.example/login", "http://scores.example?token=abc", "http://scores.example:0", "", "https://"};
        for (String input : rejected) {
            assertThrows(input, IllegalArgumentException.class, () -> UrlPolicy.normalizeService(input));
        }
    }
    @Test public void sameOriginAllowsPageQueryAndFragment() {
        assertEquals(UrlPolicy.Navigation.INTERNAL, UrlPolicy.classify("https://scores.example", "https://SCORES.example:443/fixtures?q=a#home"));
        assertEquals(UrlPolicy.Navigation.INTERNAL, UrlPolicy.classify("http://scores.example", "http://SCORES.example:80/fixtures?q=a#home"));
        assertEquals(UrlPolicy.Navigation.EXTERNAL, UrlPolicy.classify("http://scores.example", "https://scores.example/"));
        assertEquals(UrlPolicy.Navigation.EXTERNAL, UrlPolicy.classify("https://scores.example", "http://scores.example/"));
    }
    @Test public void hostSuffixAndChangedPortAreExternal() {
        String[] external = {"https://scores.example.evil.example", "https://evil.example", "https://scores.example:9443"};
        for (String input : external) assertEquals(input, UrlPolicy.Navigation.EXTERNAL, UrlPolicy.classify("https://scores.example", input));
    }
    @Test public void unsafeSchemesAndDeceptiveHostsBlocked() {
        String[] blocked = {"ftp://scores.example/", "file:///etc/passwd", "content://data", "data:text/html,test", "intent://foo", "javascript:alert(1)",
            "https://scores.example@evil.example", "https://scores.example\\evil.example", "https://scores.example./"};
        for (String input : blocked) assertEquals(input, UrlPolicy.Navigation.BLOCKED, UrlPolicy.classify("https://scores.example", input));
    }
    @Test public void lateFinishCannotHideOfflineError() {
        PageState page = new PageState(); page.started("https://scores.example/"); page.failed();
        assertTrue(page.hasFailed()); assertFalse(page.canFinish("https://scores.example/"));
        page.started("https://scores.example/"); assertTrue(page.canFinish("https://scores.example/"));
    }
    @Test public void staleDocumentCannotFinishNewNavigation() {
        PageState page = new PageState(); page.started("https://scores.example/a"); page.started("https://scores.example/b");
        assertFalse(page.canFinish("https://scores.example/a")); assertTrue(page.canFinish("https://scores.example/b"));
    }
    private static String identity(String api, String minimum, String product, String version) {
        return "{\"product_id\":\""+product+"\",\"api_version\":"+api+",\"min_android_version_code\":"+minimum+",\"version\":\""+version+"\"}";
    }
    @Test public void acceptsCompatibleServiceIdentity() throws Exception {
        assertEquals("2.0.0-dev", ServerProbe.parse(identity("2","1","ai-football-experts","2.0.0-dev"),20000).version);
    }
    @Test public void incompatibleServiceOrOldClientRejected() {
        assertThrows(IOException.class, () -> ServerProbe.parse(identity("3","1","ai-football-experts","2.0"),20000));
        assertThrows(IOException.class, () -> ServerProbe.parse(identity("2","20001","ai-football-experts","2.0"),20000));
        assertThrows(IOException.class, () -> ServerProbe.parse(identity("2","1","other-product","2.0"),20000));
    }
    @Test public void malformedIdentityRejected() {
        String[] rejected={"{}", "[]", "broken", identity("2.5","1","ai-football-experts","2.0"),identity("2","-1","ai-football-experts","2.0"),
            identity("2","\"1\"","ai-football-experts","2.0"),identity("2","1","ai-football-experts","<script>")};
        for (String input : rejected) assertThrows(IOException.class, () -> ServerProbe.parse(input,20000));
    }
    @Test public void oversizedIdentityRejectedBeforeParsing() {
        assertThrows(IOException.class, () -> ServerProbe.parse("a".repeat(ServerProbe.MAX_BYTES+1),20000));
    }
}

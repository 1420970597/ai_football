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
    @Test public void rejectMalformedOrCredentialBearingPairing() {
        String[] rejected = {"http://scores.example", "//scores.example", "javascript:alert(1)", "https://u:p@scores.example",
            "https://scores.example/user", "https://scores.example?token=abc", "https://scores.example#x",
            "https://scores.example.", "https://scores.example:", "https://scores.example:0", "https://scores.example:65536",
            "https://scores.example\\@evil.example", "https://scores.example\n.evil.example", "https://%73cores.example",
            "https://scores.example%2f.evil.example", "https://scores.example:bad", "", "https://"};
        for (String input : rejected) {
            assertThrows(input, IllegalArgumentException.class, () -> UrlPolicy.normalizeService(input));
        }
    }
    @Test public void sameOriginAllowsPageQueryAndFragment() {
        assertEquals(UrlPolicy.Navigation.INTERNAL, UrlPolicy.classify("https://scores.example", "https://SCORES.example:443/fixtures?q=a#home"));
    }
    @Test public void hostSuffixAndChangedPortAreExternal() {
        String[] external = {"https://scores.example.evil.example", "https://evil.example", "https://scores.example:9443"};
        for (String input : external) assertEquals(input, UrlPolicy.Navigation.EXTERNAL, UrlPolicy.classify("https://scores.example", input));
    }
    @Test public void unsafeSchemesAndDeceptiveHostsBlocked() {
        String[] blocked = {"http://scores.example/", "file:///etc/passwd", "content://data", "data:text/html,test", "intent://foo", "javascript:alert(1)",
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

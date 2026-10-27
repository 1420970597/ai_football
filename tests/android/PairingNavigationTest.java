package com.aifootball.platform;

import org.junit.Test;
import static org.junit.Assert.*;

public final class PairingNavigationTest {
    @Test public void qrAddressIsCanonicalCandidateOnly() {
        assertEquals("https://scores.example:9443", ScanPolicy.candidate("HTTPS://Scores.Example:9443/", "QR_CODE"));
    }
    @Test public void rejectMissingWrongFormatAndOversizedScan() {
        assertThrows(IllegalArgumentException.class, () -> ScanPolicy.candidate(null, "QR_CODE"));
        assertThrows(IllegalArgumentException.class, () -> ScanPolicy.candidate("https://scores.example", null));
        assertThrows(IllegalArgumentException.class, () -> ScanPolicy.candidate("https://scores.example", "CODE_128"));
        assertThrows(IllegalArgumentException.class, () -> ScanPolicy.candidate("a".repeat(ScanPolicy.MAX_RESULT_CHARS + 1), "QR_CODE"));
    }
    @Test public void qrCannotBypassExistingPairingPolicy() {
        String[] rejected = {"javascript:alert(1)", "http://scores.example", "https://u:p@scores.example",
                "https://scores.example/login", "https://scores.example?token=secret", "https://scores.example#section",
                "https://scores.example\\@evil.example", "intent://scores.example"};
        for (String value : rejected) assertThrows(value, IllegalArgumentException.class,
                () -> ScanPolicy.candidate(value, "QR_CODE"));
    }
    @Test public void duplicateBackWhilePendingHasNoSecondNavigation() {
        BackRequest request = new BackRequest();
        long token = request.begin();
        assertTrue(token > 0);
        assertEquals(0, request.begin());
        assertTrue(request.complete(token));
        assertFalse(request.complete(token));
    }
    @Test public void timeoutOrCancellationMakesLateCallbacksHarmless() {
        BackRequest request = new BackRequest();
        long stale = request.begin();
        assertTrue(request.complete(stale));
        long current = request.begin();
        assertNotEquals(stale, current);
        assertFalse(request.complete(stale));
        request.cancel();
        assertFalse(request.complete(current));
        assertTrue(request.begin() > current);
    }
    @Test public void zeroAndUnrelatedCallbacksCannotCompletePendingBack() {
        BackRequest request = new BackRequest();
        long token = request.begin();
        assertFalse(request.complete(0));
        assertFalse(request.complete(token + 1));
        assertEquals(0, request.begin());
        assertTrue(request.complete(token));
    }
}

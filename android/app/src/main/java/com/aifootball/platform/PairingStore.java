package com.aifootball.platform;

import android.content.Context;
import android.content.SharedPreferences;
import android.security.keystore.KeyGenParameterSpec;
import android.security.keystore.KeyProperties;
import android.util.Base64;
import java.nio.charset.StandardCharsets;
import java.security.GeneralSecurityException;
import java.security.KeyStore;
import javax.crypto.Cipher;
import javax.crypto.KeyGenerator;
import javax.crypto.SecretKey;
import javax.crypto.spec.GCMParameterSpec;

/** Only the paired origin is stored here. User/admin authentication stays in HttpOnly cookies. */
final class PairingStore {
    private static final String ALIAS = "ballnews-pairing-v1";
    private static final byte[] CONTEXT = "ballnews-origin-v1".getBytes(StandardCharsets.UTF_8);
    private final SharedPreferences preferences;
    PairingStore(Context context) { preferences = context.getSharedPreferences("pairing", Context.MODE_PRIVATE); }

    String read() throws GeneralSecurityException {
        String encrypted = preferences.getString("origin", null);
        if (encrypted == null) return null;
        try {
            byte[] data = Base64.decode(encrypted, Base64.NO_WRAP);
            if (data.length < 29) throw new GeneralSecurityException("Invalid pairing record");
            Cipher cipher = Cipher.getInstance("AES/GCM/NoPadding");
            cipher.init(Cipher.DECRYPT_MODE, key(), new GCMParameterSpec(128, data, 0, 12));
            cipher.updateAAD(CONTEXT);
            return UrlPolicy.normalizeService(new String(cipher.doFinal(data, 12, data.length - 12), StandardCharsets.UTF_8));
        } catch (IllegalArgumentException e) { throw new GeneralSecurityException("Invalid pairing record", e); }
    }

    void write(String origin) throws GeneralSecurityException {
        Cipher cipher = Cipher.getInstance("AES/GCM/NoPadding");
        cipher.init(Cipher.ENCRYPT_MODE, key());
        cipher.updateAAD(CONTEXT);
        byte[] payload = cipher.doFinal(UrlPolicy.normalizeService(origin).getBytes(StandardCharsets.UTF_8));
        byte[] data = new byte[cipher.getIV().length + payload.length];
        System.arraycopy(cipher.getIV(), 0, data, 0, cipher.getIV().length);
        System.arraycopy(payload, 0, data, cipher.getIV().length, payload.length);
        if (!preferences.edit().putString("origin", Base64.encodeToString(data, Base64.NO_WRAP)).commit()) {
            throw new GeneralSecurityException("Pairing storage unavailable");
        }
    }

    void clear() { preferences.edit().clear().apply(); }

    private SecretKey key() throws GeneralSecurityException {
        try {
            KeyStore store = KeyStore.getInstance("AndroidKeyStore");
            store.load(null);
            if (store.containsAlias(ALIAS)) return (SecretKey) store.getKey(ALIAS, null);
            KeyGenerator generator = KeyGenerator.getInstance(KeyProperties.KEY_ALGORITHM_AES, "AndroidKeyStore");
            generator.init(new KeyGenParameterSpec.Builder(ALIAS, KeyProperties.PURPOSE_ENCRYPT | KeyProperties.PURPOSE_DECRYPT)
                    .setBlockModes(KeyProperties.BLOCK_MODE_GCM).setEncryptionPaddings(KeyProperties.ENCRYPTION_PADDING_NONE).build());
            return generator.generateKey();
        } catch (java.io.IOException e) { throw new GeneralSecurityException("Keystore unavailable", e); }
    }
}

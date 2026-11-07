plugins { id("com.android.application") }

android {
    namespace = "com.aifootball.platform"
    compileSdk = 35
    buildToolsVersion = "35.0.0"
    buildFeatures { buildConfig = true }
    defaultConfig {
        applicationId = "com.aifootball.platform"
        minSdk = 26
        targetSdk = 35
        versionCode = providers.gradleProperty("platformVersionCode").orElse("20000").get().toInt()
        versionName = providers.gradleProperty("platformVersionName").orElse("2.0.0-dev").get()
    }
    buildTypes {
        debug { applicationIdSuffix = ".dev"; versionNameSuffix = "+debug" }
        release { isMinifyEnabled = false; proguardFiles(getDefaultProguardFile("proguard-android-optimize.txt"), "proguard-rules.pro") }
    }
    compileOptions { sourceCompatibility = JavaVersion.VERSION_17; targetCompatibility = JavaVersion.VERSION_17 }
    sourceSets.getByName("test").java.srcDir("../../tests/android")
}

dependencies {
    testImplementation("junit:junit:4.13.2")
    testImplementation("org.json:json:20240303")
}

dependencyLocking { lockAllConfigurations() }

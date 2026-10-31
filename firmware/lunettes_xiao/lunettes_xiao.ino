// Lunettes secrètes — Seeed XIAO ESP32-C3 ou ESP32-S3.
//
// Un seul programme, trois rôles. Choisis ROLE plus bas, puis téléverse :
//   ROLE_LUNETTES        plan A : le capteur est sur les lunettes et regarde la barre de synchro
//   ROLE_LUNETTES_RADIO  plan B, côté lunettes : reçoit le « top » de début de trame par radio
//   ROLE_EMETTEUR        plan B, côté écran : capteur scotché sur la barre, envoie le top par radio
//
// Côté lunettes, une boucle à verrouillage de phase prédit le début de chaque trame A. Un front
// manqué ou bruité ne fait donc pas perdre la synchro, et les verres restent calés sur l'écran.
//
// Carte : « XIAO_ESP32C3 » ou « XIAO_ESP32S3 » (paquet esp32 d'Espressif, version 3.x),
// « USB CDC On Boot : Enabled ».
// Testé sur XIAO ESP32-S3 + TEMT6000, rôle ROLE_LUNETTES (plan A), verres câblés en direct sur D4/D5.
// Le plan B (radio) et la XIAO ESP32-C3 compilent mais n'ont pas été testés en vrai.
// Réglages par le moniteur série (115200 bauds) : tape « ? ».

#include <WiFi.h>
#include <esp_now.h>
#include <esp_wifi.h>
#include <esp_sleep.h>
#include <esp_timer.h>
#include <Preferences.h>

#define ROLE_LUNETTES       1
#define ROLE_LUNETTES_RADIO 2
#define ROLE_EMETTEUR       3
#define ROLE ROLE_LUNETTES

// Broches : le câblage suit les inscriptions de la XIAO (D1 capteur, D4 et D5 verres), que la puce
// soit une ESP32-C3 ou une ESP32-S3. Les numéros GPIO, eux, changent d'une puce à l'autre.
#if CONFIG_IDF_TARGET_ESP32S3
const int PIN_CAPTEUR = 2;   // GPIO2 (D1) : sortie S du TEMT6000
const int PIN_VERRE_1 = 5;   // GPIO5 (D4) : IN1 du DRV8833, ou un fil des verres en direct
const int PIN_VERRE_2 = 6;   // GPIO6 (D5) : IN2 du DRV8833, ou l'autre fil
#else
// ESP32-C3 : on évite GPIO2, 8 et 9, qui décident du mode de démarrage de la puce.
const int PIN_CAPTEUR = 3;   // GPIO3 (D1) : sortie S du TEMT6000
const int PIN_VERRE_1 = 6;   // GPIO6 (D4) : IN1 du DRV8833, ou un fil des verres en direct
const int PIN_VERRE_2 = 7;   // GPIO7 (D5) : IN2 du DRV8833, ou l'autre fil
#endif

// Réglages, modifiables par le moniteur série.
float periodeNominaleUs = 8333.3f;   // une trame A toutes les 2 images à 240 Hz
long ouvreUs = 900;                  // délai entre le début de A et l'ouverture des verres
long fermeUs = 3650;                 // délai entre le début de A et leur fermeture
const int64_t PERTE_US = 300000;                  // 0,3 s sans front valable : synchro perdue
const int64_t VEILLE_US = 5LL * 60 * 1000000;     // 5 min sans synchro : veille (RESET pour réveiller)
float amplMin = 1.0f;                // seuil : amplitude filtrée (pas d'ADC) en dessous de laquelle on ignore

// ---------- Verres ----------

bool polarite = false;   // alterne à chaque fermeture : jamais de courant continu dans les cristaux

void verres(bool ouverts) {
  // Ouverts : les deux fils au même niveau, donc aucune tension. HIGH/HIGH plutôt que LOW/LOW, car
  // le DRV8833 laisserait sinon les verres déconnectés au lieu de les décharger.
  int a = HIGH, b = HIGH;
  if (!ouverts) {
    polarite = !polarite;
    a = polarite ? HIGH : LOW;
    b = polarite ? LOW : HIGH;
  }
  digitalWrite(PIN_VERRE_1, a);
  digitalWrite(PIN_VERRE_2, b);
}

// ---------- Détection des fronts (capteur) ----------
// Le capteur voit toute la lumière : l'écran, la pièce, et le petit scintillement propre à l'écran
// à chaque rafraîchissement (240 Hz). Seule la barre de synchro alterne à 120 Hz. On mesure donc à
// cadence fixe et on passe le signal dans un filtre passe-bande centré sur 120 Hz, qui ne garde que
// la barre. Au centre du filtre il n'y a aucun décalage dans le temps : quand le signal filtré
// repasse par zéro en montant, c'est le début de la trame A.

const int64_t ECHANT_US = 100;     // 10 000 mesures par seconde
const float Q_FILTRE = 10.0f;      // finesse du filtre : plus haut = plus sélectif, mais plus lent
float fb0, fb2, fa1, fa2;          // coefficients du filtre (le terme b1 vaut 0)
float fx1 = 0, fx2 = 0, fy1 = 0, fy2 = 0;
float ampl = 0;                    // amplitude du signal filtré
bool arme = false;
int64_t tProchainEch = 0;

void calculerFiltre() {
  float w0 = 2.0f * PI * (1e6f / periodeNominaleUs) * (ECHANT_US / 1e6f);
  float alpha = sinf(w0) / (2.0f * Q_FILTRE);
  float a0 = 1.0f + alpha;
  fb0 = alpha / a0;
  fb2 = -alpha / a0;
  fa1 = -2.0f * cosf(w0) / a0;
  fa2 = (1.0f - alpha) / a0;
}

// Renvoie l'instant précis du front en µs, ou -1 s'il n'y en a pas sur cette mesure.
int64_t detecterFront(int64_t t, int v) {
  float y = fb0 * v + fb2 * fx2 - fa1 * fy1 - fa2 * fy2;
  fx2 = fx1;
  fx1 = v;
  fy2 = fy1;
  float yPrec = fy1;
  fy1 = y;
  float a = fabsf(y);
  ampl += (a > ampl) ? (a - ampl) * 0.05f : -ampl * 0.0003f;   // suit les crêtes, oublie en ~0,3 s
  if (ampl < amplMin) {
    arme = false;
    return -1;
  }
  if (y < -0.3f * ampl) arme = true;
  if (arme && yPrec < 0 && y >= 0) {
    arme = false;
    // interpolation entre les deux mesures, pour dater le passage à zéro à quelques µs près
    return t - (int64_t)(ECHANT_US * (y / (y - yPrec)));
  }
  return -1;
}

// Mesure le capteur à cadence fixe. Renvoie l'instant d'un front, ou -1.
int64_t echantillonner(int64_t t) {
  if (t < tProchainEch) return -1;
  if (t - tProchainEch > 5000) tProchainEch = t;   // gros retard : on repart d'ici
  tProchainEch += ECHANT_US;
  return detecterFront(t, analogRead(PIN_CAPTEUR));
}

// ---------- Boucle à verrouillage de phase (côté lunettes) ----------

bool verrouille = false;
float periodeUs = 8333.3f;          // période estimée
double prochainA = 0;               // instant prédit du prochain début de A, en µs
int coherents = 0, incoherents = 0;
int nValides = 0;                   // fronts acceptés depuis le verrouillage
int glisse = 0;                     // fronts d'affilée décalés d'une demi-période (image sautée)
int fRecus = 0, fAcceptes = 0;      // diagnostic : part des fronts dans la porte
int64_t dernierFrontBrut = 0, dernierFrontValide = 0, dernierVerrou = 0;
float erreurMoyUs = 0;              // écart moyen entre fronts observés et prédits (diagnostic)
bool ouvrePrevu = false, fermePrevu = false;
int64_t tOuvre = 0, tFerme = 0;

void deverrouiller() {
  verrouille = false;
  coherents = 0;
  ouvrePrevu = fermePrevu = false;
  verres(true);   // sans synchro, on laisse voir normalement
}

void frontObserve(int64_t t) {
  if (!verrouille) {
    // Recherche : il faut 8 fronts d'affilée espacés d'environ une période.
    float ecart = (float)(t - dernierFrontBrut);
    dernierFrontBrut = t;
    if (fabsf(ecart - periodeNominaleUs) < 0.05f * periodeNominaleUs) {
      periodeUs = coherents == 0 ? ecart : periodeUs + (ecart - periodeUs) * 0.25f;
      if (++coherents >= 8) {
        verrouille = true;
        incoherents = 0;
        nValides = 0;
        glisse = 0;
        erreurMoyUs = 0;
        prochainA = t + periodeUs;
        dernierFrontValide = t;
      }
    } else {
      coherents = 0;
    }
    return;
  }
  // Verrouillé : écart avec le début de A prédit le plus proche.
  double err = t - (prochainA - periodeUs);
  if (err > periodeUs / 2) err -= periodeUs;
  fRecus++;
  if (fabs(err) > periodeUs / 4) {   // front aberrant (bruit, contenu de la vidéo) : ignoré
    // Image sautée par l'ordinateur : A et B s'inversent, et les fronts arrivent tous à une
    // demi-période. Après 6 d'affilée, on se recale d'une demi-période au lieu de tout reperdre.
    if (fabs(fabs(err) - periodeUs / 2) < periodeUs / 8) {
      if (++glisse >= 6) {
        prochainA += periodeUs / 2;
        glisse = 0;
        incoherents = 0;
        dernierFrontValide = t;
        return;
      }
    } else {
      glisse = 0;
    }
    if (++incoherents > 60) deverrouiller();
    return;
  }
  fAcceptes++;
  glisse = 0;
  incoherents = 0;
  dernierFrontValide = t;
  // Gains forts juste après le verrouillage pour se caler vite, puis faibles pour moins suivre le bruit.
  // Les deux baissent ensemble : baisser seulement la phase rendrait la boucle instable.
  bool calage = nValides++ < 60;
  prochainA += err * (calage ? 0.125 : 0.03);             // corrige la phase
  periodeUs += (float)err * (calage ? 0.01f : 0.0006f);   // et, doucement, la période
  periodeUs = constrain(periodeUs, 0.97f * periodeNominaleUs, 1.03f * periodeNominaleUs);
  erreurMoyUs += ((float)fabs(err) - erreurMoyUs) * 0.05f;
}

// ---------- Radio (plan B) ----------

const uint8_t DIFFUSION[6] = {0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF};
const uint32_t MAGIE = 0x4C554E45;   // « LUNE » : pour ignorer les autres appareils ESP-NOW
struct Top {
  uint32_t magie;
  uint32_t numero;
};
Top top = {MAGIE, 0};

portMUX_TYPE verrouRadio = portMUX_INITIALIZER_UNLOCKED;
volatile bool topRecu = false;
volatile int64_t tTopRecu = 0;

void surReception(const esp_now_recv_info_t *info, const uint8_t *data, int len) {
  if (len != sizeof(Top) || ((const Top *)data)->magie != MAGIE) return;
  int64_t t = esp_timer_get_time();
  portENTER_CRITICAL(&verrouRadio);
  tTopRecu = t;
  topRecu = true;
  portEXIT_CRITICAL(&verrouRadio);
}

void initRadio(bool emetteur) {
  WiFi.mode(WIFI_STA);
  esp_wifi_set_channel(1, WIFI_SECOND_CHAN_NONE);
  esp_wifi_set_ps(WIFI_PS_NONE);   // sans économie d'énergie, sinon des tops arrivent en retard
  if (esp_now_init() != ESP_OK) {
    Serial.println("ESP-NOW : échec de l'initialisation");
    return;
  }
  if (emetteur) {
    esp_now_peer_info_t pair = {};
    memcpy(pair.peer_addr, DIFFUSION, 6);
    pair.channel = 1;
    pair.encrypt = false;
    esp_now_add_peer(&pair);
  } else {
    esp_now_register_recv_cb(surReception);
  }
}

// ---------- Moniteur série ----------

bool modeTest = false;
Preferences memoire;   // les réglages survivent aux coupures de courant

void chargerReglages() {
  memoire.begin("lunettes", true);
  ouvreUs = memoire.getLong("ouvre", ouvreUs);
  fermeUs = memoire.getLong("ferme", fermeUs);
  periodeNominaleUs = memoire.getFloat("periode", periodeNominaleUs);
  amplMin = memoire.getFloat("seuil", amplMin);
  memoire.end();
}

void sauverReglages() {
  memoire.begin("lunettes", false);
  memoire.putLong("ouvre", ouvreUs);
  memoire.putLong("ferme", fermeUs);
  memoire.putFloat("periode", periodeNominaleUs);
  memoire.putFloat("seuil", amplMin);
  memoire.end();
}

void aide() {
  Serial.println("Commandes :");
  Serial.println("  o <µs>  délai entre le début de A et l'ouverture des verres");
  Serial.println("  f <µs>  délai entre le début de A et la fermeture des verres");
  Serial.println("  p <µs>  période attendue (8333 pour un écran 240 Hz, 13889 pour 144 Hz)");
  Serial.println("  a <n>   seuil de détection (1 par défaut ; plus bas = capte de plus loin, mais plus de bruit)");
  Serial.println("  t       mode test : les verres clignotent à 1 Hz (vérifie qu'ils noircissent)");
  Serial.println("  ?       cette aide");
}

void commande(char *s) {
  dernierVerrou = esp_timer_get_time();   // on est en train de régler : pas de mise en veille
  switch (s[0]) {
    // o et f doivent tenir dans une période, sinon la trame suivante écrase l'ouverture ou la fermeture.
    case 'o': if (atol(s + 1) > 0 && atol(s + 1) < 0.95f * periodeNominaleUs) ouvreUs = atol(s + 1); break;
    case 'f': if (atol(s + 1) > 0 && atol(s + 1) < 0.95f * periodeNominaleUs) fermeUs = atol(s + 1); break;
    case 'p':
      if (atof(s + 1) > 1000) {
        periodeNominaleUs = atof(s + 1);
        periodeUs = periodeNominaleUs;
        calculerFiltre();
        deverrouiller();
      }
      break;
    case 'a': if (atof(s + 1) > 0) amplMin = atof(s + 1); break;
    case 't': modeTest = !modeTest; verres(true); break;
    case '?': aide(); break;
  }
  if (s[0] == 'o' || s[0] == 'f' || s[0] == 'p' || s[0] == 'a') sauverReglages();
  Serial.printf("ouverture %ld µs, fermeture %ld µs, période attendue %.1f µs, seuil %.2f%s\n",
                ouvreUs, fermeUs, periodeNominaleUs, amplMin, modeTest ? "  (MODE TEST)" : "");
}

void lireSerie() {
  static char buf[32];
  static int n = 0;
  while (Serial.available()) {
    char c = Serial.read();
    if (c == '\n' || c == '\r') {
      buf[n] = 0;
      if (n) commande(buf);
      n = 0;
    } else if (n < 31) {
      buf[n++] = c;
    }
  }
}

// ---------- Programme principal ----------

int frontsSeconde = 0;
int64_t tStat = 0;

void setup() {
  Serial.begin(115200);
  pinMode(PIN_VERRE_1, OUTPUT);
  pinMode(PIN_VERRE_2, OUTPUT);
  verres(true);
  analogReadResolution(12);
  chargerReglages();
  periodeUs = periodeNominaleUs;
  calculerFiltre();
#if ROLE == ROLE_EMETTEUR
  initRadio(true);
#elif ROLE == ROLE_LUNETTES_RADIO
  initRadio(false);
#endif
  dernierVerrou = esp_timer_get_time();
  delay(500);
  aide();
}

void loop() {
  int64_t t = esp_timer_get_time();
  lireSerie();

  if (modeTest) {
    static int64_t tTest = 0;
    static bool ouverts = true;
    if (t - tTest > 500000) {
      tTest = t;
      ouverts = !ouverts;
      verres(ouverts);
    }
    return;
  }

#if ROLE == ROLE_EMETTEUR
  if (echantillonner(t) >= 0) {
    top.numero++;
    esp_now_send(DIFFUSION, (const uint8_t *)&top, sizeof(top));
    frontsSeconde++;
  }
  if (t - tStat >= 1000000) {
    Serial.printf("[émetteur] tops envoyés/s %d (attendu ~120)   amplitude %.1f\n", frontsSeconde, ampl);
    frontsSeconde = 0;
    tStat = t;
  }
  return;
#else

#if ROLE == ROLE_LUNETTES
  int64_t tFront = echantillonner(t);
  if (tFront >= 0) {
    frontsSeconde++;
    frontObserve(tFront);
  }
#else
  bool recu;
  int64_t tr;
  portENTER_CRITICAL(&verrouRadio);
  recu = topRecu;
  tr = tTopRecu;
  topRecu = false;
  portEXIT_CRITICAL(&verrouRadio);
  if (recu) {
    frontsSeconde++;
    frontObserve(tr);
  }
#endif

  // Début de A prédit : on programme l'ouverture et la fermeture des verres.
  if (verrouille) {
    dernierVerrou = t;
    if (t >= prochainA) {
      while (t >= prochainA + periodeUs) prochainA += periodeUs;   // gros retard : on saute les trames ratées
      int64_t debutA = (int64_t)prochainA;
      prochainA += periodeUs;
      tOuvre = debutA + ouvreUs;
      tFerme = debutA + fermeUs;
      ouvrePrevu = fermePrevu = true;
    }
    if (t - dernierFrontValide > PERTE_US) deverrouiller();
  }
  if (ouvrePrevu && t >= tOuvre) { verres(true); ouvrePrevu = false; }
  if (fermePrevu && t >= tFerme) { verres(false); fermePrevu = false; }

  if (t - tStat >= 1000000) {
#if ROLE == ROLE_LUNETTES
    Serial.printf("[lunettes] fronts/s %d (attendu ~120)   amplitude %.1f   ", frontsSeconde, ampl);
#else
    Serial.printf("[lunettes radio] tops reçus/s %d (attendu ~120)   ", frontsSeconde);
#endif
    Serial.printf("%s   période %.1f µs   écart moyen %.0f µs   dans la porte %d %%\n",
                  verrouille ? "VERROUILLÉ" : "recherche...", periodeUs, erreurMoyUs,
                  fRecus ? 100 * fAcceptes / fRecus : 0);
    frontsSeconde = 0;
    fRecus = fAcceptes = 0;
    tStat = t;
  }

  // Pas de veille tant qu'un moniteur série est ouvert sur l'ordinateur : on est en train de régler.
  if (!verrouille && !Serial && t - dernierVerrou > VEILLE_US) {
    Serial.println("Pas d'écran depuis 5 min : mise en veille. Appuie sur RESET pour réveiller.");
    Serial.flush();
    verres(true);
    esp_deep_sleep_start();
  }
#endif
}

# Raspberry Pi 5 canlı ölçüm notları

1 Ekim 2026 tarihinde Debian 13 ARM64, kernel
`6.12.47+rpt-rpi-2712` ve bpftrace `0.23.2` üzerinde çalıştırıldı.
Hedef, makinede zaten çalışan `rrdcached` servisiydi. Önceki oturumda PID
1120, sonraki oturumda PID 1016 kullanıldı; her ölçümden önce süreç kimliği
yeniden kontrol edildi.

Servis doğal hâlinde büyük ölçüde boşta olduğundan, UNIX soketine salt okunur
`STATS` komutları gönderildi. Bu kontrollü yük, servisin normal kullanımını
temsil etmez. RRD verisine yazılmadı ve servis yeniden başlatılmadı.
İlk denemede her istek yeni bağlantı açtı; sonraki denemede dört kalıcı
bağlantı kullanıldı. Yük betikleri ölçüm bitince durduruldu ve servis tekrar
8 kalıcı iş parçacığına döndü.

## Yerel paketler

Paketler çalışma alanındaki, Git tarafından dışlanan `runs/pi5-live/real/`
dizininde tutuluyor. Paket adları UTC ölçüm başlangıcını içerir.

| Paket etiketi | Profil | CPU örneği | Bilinmeyen frame | Tahmini ek yük | Durum |
|---|---|---:|---:|---:|---|
| `20260930T211648Z-rrdcached-stats` | standard + PMU | 526 | %13,2 | %37,7 | partial |
| `20260930T211905Z-rrdcached-light` | light | 484 | %12,7 | %42,6 | ok |
| `20261001T125119Z-rrdcached-persistent-v2` | standard + PMU | 217 | %20,6 | %21,1 | partial |
| `20261001T125340Z-rrdcached-persistent-light-v2` | light | 164 | %21,6 | %6,0 | ok |
| `20261001T130337Z-rrdcached-pmu-fixed-v2` | standard + PMU | 228 | %18,5 | %24,4 | partial |

Her paket şema ve SHA-256 dosya bütünlüğü denetiminden geçti. `ok`, bütün
istenen probların çıktı ürettiğini belirtir; ölçüm etkisinin düşük veya tüm
fonksiyonların çözümlenmiş olduğu anlamına gelmez.

## Verinin yorumlanması

İlk bağlantı yükünde CPU yığınlarında `pthread_create → clone3`,
`poll → ppoll` ve thread çıkışı öne çıktı. 45 saniyede yaklaşık 76–82 bin
yaratım kaydedildi; bunların hemen tamamının gözlenen ömrü 1 ms'den kısaydı.
Bu hareket kontrollü bağlantı yüküyle uyumludur. Eski `threadlife` filtresi
worker tarafından açılan bazı thread'leri kaçırabildiğinden yaratım/çıkış
farkı bir thread sızıntısı kanıtı değildir.
Bu yükte PMU'nun 200 ms'de bir yeni thread araması kısa ömürlü thread'leri
kaçırdı. İlk standard pakette 76.278 yaratımına karşı yalnız 19 PMU satırı
vardı; o paketin PMU toplamları süreç toplamı olarak yorumlanmamalıdır.

Kalıcı bağlantılı standard ölçümünde yeni thread yaratımı veya çıkışı
olmadı. Off-CPU toplamı `244.590.378 µs` idi; bu, iş parçacıkları üzerinde
toplanan bekleme süresidir ve 45 saniyelik duvar süresini aşabilir.
`futex` smoke denemesi olay üretmedi; kilit çekişmesi hakkında sonuç
çıkarılamaz. PMU'nun `partial` sonucu ayrıca incelendi: 6 etkin thread'in
sayaçları etkin sürelerinin tamamında çalışmıştı. CPU'da hiç çalışmayan 6
thread'in sıfır sayaçları yanlışlıkla yetersiz sayaç çalışma süresi sayılıyor,
toplamlar ve IPC bu yüzden gizleniyordu. Aşağıdaki PMU düzeltmesi bu sorunu
giderir; önceki paket değiştirilmedi.

Düzeltmeden sonraki `130337Z` kaydında PMU `ok`: 6 boşta thread'in
sayaçları sıfır, 6 etkin thread'in sayaçlarında çalışma oranı %100. Toplam
`736.677.724` cycle ve `794.171.707` instruction üzerinden kullanıcı alanı
IPC'si `1,08` hesaplandı. Run queue histogramında `79.116` hedef örneği,
yaklaşık `6,04 µs` ortalama bekleme görüldü. Thread yaratımı/çıkışı olmadı.
Kayıt yalnız olay üretmeyen `futex` nedeniyle `partial`; bu durum PMU
sonuçlarını geçersiz kılmaz. Yük boyunca toplam `162.250` STATS isteği
hatasız tamamlandı; bu sayı preflight ve başlangıç/son CPU örneklemesini de
içerir, yalnız 45 saniyelik toplama penceresine ait değildir.

Kalıcı bağlantılı `light` ölçümünde de thread yaratımı veya çıkışı olmadı.
Tahmini ek yük %6,0 idi. Bu değer hedefin CPU kullanımından hesaplanır;
başlangıç CPU kullanımı %10'un altındaysa hesaplamanın paydası %10'a
tamamlanır. Profiller farklı olaylar topladığı ve koşular farklı zamanlarda
alındığı için aradaki fark bir uygulama hızlanması kanıtı değildir.

Bilinmeyen frame oranı uygulamaya özgü sıcak fonksiyonu güvenle
adlandırmayı sınırlar. Görülen libc/kernel yolları, çözümlenemeyen
uygulama frame'lerinin yerini tutmaz. Farklı ölçüm etkilerine sahip bu
paketler performans iyileşmesi kanıtı olarak karşılaştırılmamalıdır.

## Bu testte düzeltilen sorunlar

- Ölçülmeyen veya dengesiz CPU başlangıcına dayanan ek yük artık sürüm 2
  paketlerde `null` kaydediliyor. Viewer ve `inspect`, eski paketlerdeki
  hatalı `0.0%` değerini kalite notlarından tanıyıp `n/a` gösteriyor.
- Sürüm 1 `runqlat` probu sistem genelindeki görevleri topluyordu.
  Eski histogramların tamamı güvenilmez olarak işaretleniyor. Bazı eski
  paketlerde 45 saniyelik ölçüm içinde 35 dakikayı aşan kovalar da vardı.
- Yeni `runqlat`, hedefin TID'lerini izliyor; ayrı fork edilen süreçler ve
  idle TID 0 kapsam dışında. Çıkıştan sonra kalan TID kayıtları temizleniyor.
  Ölçüm başlarken zaten uyuyan bir thread'in ilk uyanışı kaçabilir.
- `threadlife` yaratım filtresi ana TID yerine hedef TGID'yi kullanıyor.
  Canlı kontrolde bir worker'ın açtığı 120 thread için yaratım ve çıkış
  sayıları eksiksiz 120 olarak doğrulandı.
- PMU, etkinleştirilmiş bir thread'in tüm sayaç grupları sıfırsa onu boşta
  kabul ediyor; geçerli süreç toplamlarını sıfırlamıyor. Etkin süre olup
  çalışma süresi olmaması, yalnız bazı grupların sıfır olması ve hiç
  etkinleştirilmemiş kayıtlar hâlâ `partial` olarak işaretleniyor.

Linux tam test takımı 414 testte geçti (1 atlama); son raporlama değişiklikleri
29 hedefli Linux testiyle ayrıca doğrulandı. Son PMU düzeltmesi 9 testle hem
Mac hem Linux üzerinde doğrulandı. Güncel viewer 103 testten geçti.

## Aynı isimli 120 thread kontrolü

`20261001T175652Z-same-name-120-tids` kaydı, kimlik düzeltmesini doğrulamak
için oluşturulan ayrı bir sentetik Linux hedefidir; `rrdcached` ölçümü değildir.
20,1 saniyelik pencerede 120 worker'ın tamamı `worker` adıyla çalıştı.
Başlangıç ve bitiş envanteri aynı 121 TID'yi içerdi: 120 worker ve ana thread.

- On-CPU: 120/120 worker TID, 763 çağrı yolu, 5.368 örnek.
- Off-CPU: 120/120 worker TID, 225 çağrı yolu.
- Eksik veya hedef dışı worker TID: 0.
- Ana thread `join` içinde bekledi; stack örneği uydurulmadı.
- Sürüm 2 şema ve 11 dosyanın SHA-256 kontrolü geçti.
- Test hedefi kapatıldı; ardından hedef PID ve bpftrace süreci kalmadı.

Güncel collector her iki stack dosyasında `worker [tid=123]` kökünü
kullanır. Önceki ölçümlerde aynı ad ve çağrı yolu birleştiğinden onları
yeniden TID'lere ayırmak mümkün değildir; yeni ölçüm gerekir.
Thread kimliğinin iki koşu arasında değişmesi Diff'e sahte değişim
eklemez: koşular thread adına göre karşılaştırılır.
Bu değişiklikler Linux'ta 152 hedefli regresyon testiyle doğrulandı.

## Ölçüm sonunda bekleyen 120 thread kontrolü

`20261001T182953Z-open-wait-120-tids` kaydında 120 worker önce CPU/sleep
işi yaptı, ardından ölçüm bitene kadar `pipe_read` içinde kaldı. Hedefin
başlangıç ve bitiş envanteri 121 thread'di. Kayıt 4 Ekim'de güncel parser ile
yeniden doğrulandı; bu, yeni bir canlı Linux koşusu değildir.

- On-CPU: 120/120 worker, 521 çağrı yolu, 2.611 örnek.
- Off-CPU: 120/120 worker, 632 çağrı yolu, ana thread ile 121 TID.
- Tamamlanmış beklemelerde worker `pipe_read` yolu yoktu; açık aralıklar ve
  son folded dosyası 120/120 worker'ın bu yolunu korudu.
- 121 açık aralıkta `1.857.580.308 µs`, tamamlanan aralıklarda
  `601.735.901 µs` vardı. Folded ve task-state toplamlarının ikisi de
  `2.459.316.209 µs`; çift sayım görülmedi.
- Açık worker beklemeleri 15,342–15,362 saniyeydi. Son checkpoint'e kadar
  gözlenen süre yazıldı; beklemelerin tam süresi henüz bilinmiyordu.
- Süre histogramındaki 30.315 tamamlanmış beklemeye açık aralık eklenmedi.
- Sürüm 2 şema ve 11 dosyanın SHA-256 kontrolü geçti. Kayıt yalnız olay
  üretmeyen `futex` nedeniyle `partial`; değişen yük nedeniyle ek yük `n/a`.
- Hedef normal kapandı; temizleme günlüğünde PID'nin artık bulunmadığı doğrulandı.

Bu test, yalnız kapanan off-CPU aralıklarını yazmanın uzun beklemeleri
kaybettirdiğini doğruladı. Collector artık gözlenen açık aralıkları da
koruyor; ölçümden önce uyuyan thread'ler için stack uydurmuyor.

Başlangıçta yalnız iki saniye süreç canlılığına bakmak da yeterli değildi:
bpftrace o sırada hâlâ derleniyor/yükleniyor olabilir. Her probun çalışan
eBPF timer'ından `PERFORMER_READY` gelmeden toplama süresi başlamıyor.
Hazır olmayan problar zaman aşımında birlikte durduruluyor. `/proc` thread
snapshot'ları da derleme ve map yazma süresini CPU delta'larına katmamak
için toplama penceresine taşındı.

Viewer tüm envanter satırlarını, veride bulunan TID'leri, çizilebilen kökleri
ve etikete yetecek genişlikteki kökleri ayrı gösteriyor. Küçük thread'in
**show this thread** düğmesi paylaşım eşiğini kaldırıp TID'ye odaklanıyor.
`--oncpu-hz` ile 1–4000 Hz seçilebilir; varsayılan 99 Hz korunur. Daha yüksek
oran ek yük uyarısıyla kaydedilir ve her thread'in örnekleneceğini garanti etmez.

4 Ekim doğrulamasında Linux makinesi SSH üzerinden erişilemiyordu. Yeni
canlı kayıt yerine yukarıdaki paket yeniden okundu; güncel Linux regresyonları
yerel Linux konteynerinde, viewer ise kaynak/SSR testleriyle kontrol edildi.
Ubuntu 24.04 ARM64 / Python 3.12.3 üzerinde 463 test geçti (1 atlama);
viewer'ın 128 testi, TypeScript kontrolü ve production build'i geçti. Docker'ın
sunduğu HTML, yerel build ile aynıydı. Linux testlerinde ayrıca yabancı x86
syscall başlıklarının ARM64 numaralarını yanlış adlandırabildiği görüldü;
başlık seçimi native mimariye göre yapılıyor ve iki mimari için regresyon testi var.

## Viewer'da açma

Güncel `viewer/dist/index.html` dosyasını tarayıcıda açıp **Runs → choose
files** ile paketleri seçin. Önce **Overview** kalite uyarılarına, sonra
**Flame**, **Threads** ve standard paket için **Wakeups** ekranlarına bakın.
Sayfa yenilendiğinde paketler yeniden seçilmelidir.
Üst bilgi `bundles v1–v2` ve build kimliğini gösterir. Yalnız sürüm 1
okuduğunu söyleyen sayfa eski bir viewer'dır; manifest'i değiştirmek yerine
güncel `viewer/dist/index.html` dosyasını açın veya Docker servisini
`docker compose up -d --build --force-recreate viewer` ile yenileyin.

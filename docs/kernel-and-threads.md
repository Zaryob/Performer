# Kernel modülü ve thread yaşam döngüsü

## Kernel modülünü kaydetmek

Modül yüklüyken, [örnek JSON yapılandırmasındaki](examples/kernel-trace.json)
modül, fonksiyon ve tracepoint isimlerini kendi modülüne göre değiştir.
İhtiyaç duymadığın `functions`, `tracepoints` veya `requests` listelerini
silebilirsin. En az bir izleme noktası seçilmeli; wildcard kabul edilmez.

```sh
sudo ./collector/bin/performer preflight --pid 12345 --profile light \
  --kernel-config docs/examples/kernel-trace.json
sudo ./collector/bin/performer collect --pid 12345 --duration 30 \
  --profile light --label driver-check --out runs \
  --kernel-config docs/examples/kernel-trace.json
```

`--pid`, userspace ölçümünün hedefidir. **Kernel seçimi sistem genelindedir**:
başka süreçler, workqueue çalışanları ve interrupt bağlamları da kapsanır.
Sonuçlar hedef sürece atfedilmez. Bu ilk sürüm, userspace hedefiyle birlikte
kernel ölçümü ekler; bağımsız, PID'siz bir kernel collector sunmaz.
Kernel yapılandırması CLI'dan verilir; daemon'ın ölçüm formuna eklenmemiştir.

Görüntüleyicide **Kernel** ekranı şunları gösterir:

- Seçilen fonksiyonlar için giriş ve eşleşmiş dönüş sayısı; toplam, ortalama ve
  en yüksek **kapsayıcı geçen süre**. Bu süre CPU süresi değildir; scheduler
  beklemelerini ve kesmeleri içerir. İç içe fonksiyon süreleri örtüşebilir.
- Tracepoint sayımları ve zaman damgalı olay listesi.
- İstek kimliğiyle başlangıç/tamamlanma eşleştirmesi ve gecikmeleri. Tamamlanma
  başka bir thread veya CPU'da olabilir; eşleştirme PID/TID üzerinden yapılmaz.

Fonksiyon giriş ve dönüşleri aynı ölçüm penceresinde gözlenmiş olmalıdır.
Pencereyi aşan çağrılar için süre uydurulmaz; eşleşmeyen uçlar tabloda ayrı
sayılır. Fonksiyon süresi istek gecikmesiyle aynı şey değildir: kuyruğa iş
bırakıp dönen bir fonksiyon, asenkron işi tamamlamış olmaz.

### Modüldeki özel tracepoint'ler

`requests` içindeki `start` ve `end`, `TRACE_EVENT` ile tanımlanmış iki farklı
`subsystem:event` adıdır. İkisinin de `key` isimli, olaya özgü **tamsayı alanı**
olmalıdır; `common_pid` gibi ortak tracepoint başlık alanları kabul edilmez.
Örneğin `__field(u64, request_id)` ile eklenen alan, başlangıçta ve tamamlanmada
aynı kimliği taşımalıdır. Anahtar, her adlandırılmış istek türünde eşzamanlı
bekleyen istekler arasında benzersiz olmalıdır. Aynı kimliğin tamamlanmadan
tekrar kullanılması uyarı üretir ve ölçümü `partial` yapar.
`tracepoints` ise yalnız sayım ve olay zamanını kaydeder; keyfi alanları veya
istek içeriğini dışarı aktarmaz. Tracepoint'lerin seçilen modüle ait olması
yapılandırmayı yazanın sorumluluğudur; kernel subsystem adı modül adıyla aynı
olmak zorunda değildir.

Mevcut olayları ve alanlarını Linux üzerinde kontrol edebilirsin:

```sh
sudo bpftrace -l 'tracepoint:my_driver:*'
sudo cat /sys/kernel/tracing/events/my_driver/request_start/format
```

Linux belgeleri: [TRACE_EVENT ve tracepoint kullanımı](https://docs.kernel.org/trace/tracepoints.html).

### Uyumluluk ve sınırlar

Ubuntu 22.04'teki bpftrace 0.14 için fonksiyonlar `kprobe:fonksiyon` ve
`kretprobe:fonksiyon` ile bağlanır. Collector önce `/proc/kallsyms` üzerinden
her seçilen fonksiyonun tek bir tanımının olduğunu ve istenen modüle ait
olduğunu doğrular. Belirsiz, inline edilmiş, kaldırılmış veya probe edilemeyen
fonksiyonlar için açık tracepoint kullan. Modülü ölçüm boyunca yüklü tut.

Fonksiyonlar için aynı TID'deki iç içe girişler 16 seviyeye kadar eşleştirilir;
idle bağlamındaki TID 0 her CPU için ayrılır. Sınır aşımı `partial` olarak
bildirilir. NMI iç içe çalışması için eksiksizlik garantisi verilmez.
BPF map'leri 8192 anahtarla sınırlıdır; çok sayıda eşzamanlı istek veya çağrı
bu kapasiteyi aşabilir. Eşleşmeyen uçlar hem pencere sınırlarından hem kayıptan
kaynaklanabileceğinden, sıfır kayıp kanıtı sayılmaz.

Kernel tracing, seçilen fonksiyon/olay sıklığına göre ek yük oluşturur;
`light` profilinin önceki overhead beklentisi bu ek probları kapsamaz.
Sadece ihtiyaç duyulan noktaları ve kısa ölçümleri seç.

## Thread oluşturma ve sonlanma olayları

`light`, `standard` ve `deep` profilleri thread yaşam döngüsünü kaydeder.
`task:task_newtask` olayının `CLONE_THREAD` bayrağı kontrol edilir; `fork()`
ile açılan ayrı süreçler thread oluşturma sayımına katılmaz.

**Threads → Thread lifecycle events** listesinde:

- Oluşturulma/sonlanma zamanı, TID, gözlenen isim ve oluşturan TID.
- Ölçümde gözlenen doğum zamanıyla thread nesli; TID tekrar kullanıldığında
  kayıtlar ayrı kalır.
- Hem doğumu hem sonlanması gözlenen thread'lerin yaşam süresi.

Başlangıç ve bitiş `/proc` envanterinde olmayan kısa ömürlü thread'ler de olay
listesinde yer alır. Envanter CPU sayıları bunlar için uydurulmaz. Başlangıçtan
önce doğan thread'in doğum/parent bilgisi boş kalır; bitişten sonra yaşayan
thread için sonlanma olayı üretilmez. İsim değişiklikleri olay anındaki isimle
kaydedilir. Oluşturan çağrı zinciri bu sürümde tutulmaz.

Thread olayları ve kernel olayları için ayrı ayrı yaklaşık 2048 kayıt bütçesi
CPU sayısına bölünür. Bir CPU kendi payını tüketince diğer CPU'ların boş payını
kullanmaz. Ek olaylar sayılır ancak listede tutulmaz; uyarı, `truncated` ve
`partial` durumu kaybı bildirir. Thread sayımları/yaşam süresi histogramı ve
kernel toplulaştırmaları devam eder. Çok yüksek churn hızında olay listesinin
tüm olayları içerdiği varsayılmamalıdır.

## Paket içeriği

Ek alanlar bundle v2 ile uyumludur; eski paketlerde bulunmaları gerekmez:

- `meta/kernel_trace.json`: seçimin ve sistem geneli kapsamın kaydı.
- `hist/kernel_functions.json`, `hist/kernel_requests.json`,
  `hist/kernel_tracepoints.json`: toplulaştırmalar.
- `hist/kernel_events.json`, `hist/thread_events.json`: zaman sıralı olaylar.

Olay zamanları `CLOCK_BOOTTIME` nanosaniyeleridir. JSON ve JavaScript'te
hassasiyet kaybetmemeleri için mutlak nanosaniye ve istek kimlikleri ondalık
**string** olarak tutulur. `offset_ms`, ortak ölçüm başlangıcından geçen
milisaniyedir. Thread `birth_ns`, yalnız doğum gözlendiyse doludur.
Bu dosyalar mevcut `kind: table` şemasını kullanır. Üretilen kernel probu da
`raw/kernel.bt` içinde korunur.

## Doğrulama

```sh
make check
(cd viewer && npm test && npm run build)
# Linux makinede, gerçek eBPF erişimiyle:
sudo python3 tests/threadlife_live_smoke.py
```

Canlı smoke testi 20 kısa ömürlü thread'i oluşturup sonlandırır; ayrıca bir
alt süreç başlatıp onun thread sayılmadığını doğrular. Docker'da kernel PID
ad alanıyla eşleşmek için host PID namespace gerekir. Test gerçek kernel
modülünün işlevini veya doğruluğunu doğrulamaz.

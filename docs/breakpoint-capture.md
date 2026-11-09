# İki breakpoint arasında kayıt almak

Performer, probları **kayıt kapalıyken** hazırlayıp yerel bir Unix soketinden
başlatma/durdurma komutlarını bekleyebilir. GDB'deki Python komutları bu arayüzü
kullanır. Bir collector çalışması tek bir kayıt aralığı üretir.

## İki konumu seçerek otomatik kayıt

Önce programı GDB ile başlat veya bağlan; örneğin `start` ile `main` içinde
durdur. `info inferiors` ile programın gerçek PID'sini öğren. Program henüz ilk
kayıt konumunu geçmemiş olmalıdır.

Başka bir terminalde (bu örnekte hedef PID `12345`):

```sh
sudo ./collector/bin/performer collect --pid 12345 --profile standard \
  --label region --out runs --duration 300 \
  --control-socket /tmp/performer-12345.sock --control-timeout 600
```

`control ready: ...` satırını bekle. Problar yüklenmiştir ancak henüz olay
kaydedilmez. GDB'de, depo yolunu kendi ortamına göre değiştirerek:

```gdb
source /path/to/Performer/debugger/performer-gdb.py
performer-connect /tmp/performer-12345.sock
performer-record-between 'worker.cc:120' 'worker.cc:180'
continue
```

İlk konumda GDB bütün thread'leri durdurur, collector'ın kaydı açtığını
onaylamasını bekler ve otomatik devam eder. İkinci konumda kayıt kapatılır;
**GDB ikinci konumda durur**. Collector haritaları yazıp `.tgz` paketini
oluştururken GDB'de inceleme yapabilirsin. Sonuç, collector terminalinde yazan
paket yolundadır; görüntüleyicinin normal ekranlarıyla açılır.

Konumlar fonksiyon adı, `dosya:satır` veya GDB'nin kabul ettiği adres/konum
ifadeleri olabilir. Boşluk içeren konumları tırnak içine al. İkinci breakpoint,
ilki kaydı başlatana kadar kapalıdır. İki breakpoint tek kullanımlıktır.
Başka bir aralık için yeni collector başlatıp yeniden `performer-connect` yap.

Debian paketi kurulmuşsa CLI `performer` olarak kullanılır; GDB yardımcısı:

```gdb
source /usr/share/performer/debugger/performer-gdb.py
```

## Zaten ilk breakpoint'te durmuşken

Collector'ı yukarıdaki gibi hazırlayıp GDB'yi bağladıktan sonra:

```gdb
break worker.cc:180
performer-start
continue
# İkinci breakpoint'te durduğunda:
performer-stop
```

Elle komutlar arasında beklemek de kayıt aralığına girer. Otomatik iki-konum
komutu bu kullanıcı beklemesini önler. Mevcut breakpoint numaralarına komut
listesi de bağlayabilirsin; burada `4` başlangıç, `5` bitiş breakpoint'i:

```gdb
commands 4
  silent
  performer-start
  continue
end
commands 5
  performer-stop
end
```

Bu yöntem kendi breakpoint koşullarını ve thread filtrelerini korumanı sağlar.
Başlatma başarısız olursa GDB komut listesi hata verir ve `continue` çalışmaz.

GDB yardımcısı dışında aynı protokol CLI'dan kullanılabilir:

```sh
./collector/bin/performer control status --socket /tmp/performer-12345.sock --pid 12345
./collector/bin/performer control start --socket /tmp/performer-12345.sock --pid 12345
./collector/bin/performer control stop --socket /tmp/performer-12345.sock --pid 12345
```

## Ölçüm sınırlarının anlamı

**Bunlar tam instruction veya breakpoint zaman damgaları değildir.** Kaydı
GDB ile collector arasındaki onaylı komutlar açıp kapatır. Başlangıç komutu,
bütün probların ortak gelecekteki başlangıcı aldığını doğruladıktan sonra
GDB'ye döner. Bitiş komutu, normalde yaklaşık 10 ms ilerideki ortak bitişi
ayarlar; çekirdekteki kontrol çağrısının bu bitişten önce çalıştığı ve bütün
probların doğru sınırları aldığı doğrulanır. Onayın userspace'e ulaşması daha
geç olabilir. Bu doğrulama başarısızsa ölçüm `partial` olur ve neden kaydedilir.

GDB'nin durdurması, komut iletimi, scheduler ve yeniden devam ettirme süreleri
özellikle kısa aralıklarda bekleme ölçümünü etkileyebilir. Aradaki başka
breakpoint'ler, `step`/`next` veya elle bekleme otomatik çıkarılmaz. Tek
thread'i kilitleyen scheduler ayarları da programın normal davranışını değiştirir.

`meta/window.json` ortak kayıt sınırlarını ve `control` altında istemci
komut zamanları, collector alma/onay zamanları ve bitiş nedenini taşır.
**Overview → capture control**, komut istekleri ile kayıt sınırları arasındaki
ölçülen gecikmeleri gösterir. Bu gecikmeler breakpoint'e erişildiği anın
kesin ölçümü veya debugger'ın tüm etkisinin hesabı değildir.

Performer seçilen profilin örneklerini ve olaylarını toplar; çalıştırılan her
userspace instruction'ını veya bütün fonksiyon girişlerini kaydetmez. Çok kısa
aralıklarda 99 Hz CPU örneklemesi hiç örnek üretmeyebilir. Kernel seçimi
`--kernel-config` ile eklenebilir; bu veriler aynı zaman aralığında fakat
sistem genelinde toplanır.

## Duraklatılmış hedef, sınırlar ve hatalar

Bu modda preflight, araç/yetki/PID/prob dosyalarını kontrol eder. Durmuş hedefte
anlamsız olacak deneme örneklemesi ve overhead baseline'ı çalıştırılmaz.
Gerçek attachment BPF timer'larıyla doğrulanır; stack kalitesi kayıt sonunda
elde edilen örneklerle değerlendirilir. Örnek yoksa kalitenin bilinmediği
belirtilir. Debugger varken overhead tahmini üretilmez.

- Python destekli **yerel Linux GDB** ve all-stop modu gerekir. Yardımcı yanlış
  PID'yi veya non-stop modunu kabul etmez. `gdbserver` ile başka makineye/başka
  PID namespace'ine uzaktan bağlanma bu arayüzde desteklenmez.
- Soket yalnız hedef süreç sahibinin ve root'un erişebileceği `0600` izinleriyle
  oluşturulur. Collector başka bir soketi veya mevcut dosyayı üzerine yazmaz;
  kullanılmayan kısa bir yol seç. Parent dizine GDB kullanıcısının erişebilmesi
  gerekir.
- `--control-timeout` yalnız kayıt başlatılmadan önceki beklemeyi sınırlar
  (varsayılan 300 s; en fazla 3600 s). `--duration` kayıt başladıktan sonraki
  güvenlik sınırıdır; profile ait süre limiti normal şekilde uygulanır.
- İkinci breakpoint gelmeden süre dolarsa, süreç çıkarsa veya Ctrl-C verilirse
  kayıt varsa `partial` paket yazılır. Başlatılmadan iptal/zaman aşımı olursa
  ölçüm paketi üretilmez; problar ve soket temizlenir.
- `performer-stop` onayı kaydın kapandığını bildirir. Paket yazımı bundan sonra
  tamamlanır; collector terminalindeki `bundle:` satırını bekle.
- `performer-disconnect` etkin kaydı durdurup GDB yardımcısını sıfırlar.
  Başarısız veya süresi dolmuş bir kayıt sonrası yerel durumu temizlemek için
  `performer-disconnect --force` kullanılabilir. Ulaşılamayan collector'ın kendi
  güvenlik limiti geçerliliğini korur.

GDB kaynakları: [breakpoint command lists](https://sourceware.org/gdb/current/onlinedocs/gdb.html/Break-Commands.html),
[all-stop davranışı](https://sourceware.org/gdb/current/onlinedocs/gdb.html/All_002dStop-Mode.html).

## Doğrulama

`tests/test_control.py` gerçek Unix IPC ile hazır/bekliyor/kayıt/durdurma
akışını, duraklatılmış hedefi, zaman aşımını, iptali ve erken çıkışı test eder.
`tests/test_gdb.py`, gerçek GDB ve C++ hedefinde iki breakpoint akışını çalıştırır;
yalnız eBPF süreci test double'ıdır. GDB olmayan ortamlarda bu test atlanır.
Ubuntu CI test işlerinde GDB kurulur.

Yerel ek canlı doğrulamada Ubuntu 22.04 konteynerindeki bpftrace 0.14 ve gerçek
GDB ile 350 ms CPU işi yapan bir worker iki breakpoint arasında kaydedildi:
0.36 s ortak pencere, 34 on-CPU örneği, iki thread yaşam döngüsü olayı; üç light
probu da `ok` ve pencere `certified` çıktı. Docker VM kerneli
`6.12.76-linuxkit` idi; eski bpftrace için tracepoint integer typedef'leri bir
include dosyasıyla sağlandı. Bu, Ubuntu 22.04'ün yerel kernelinde veya her
uygulamada eksiksiz ölçüm doğruluğu/overhead kanıtı değildir.

```sh
make target
python3 -m unittest tests.test_control tests.test_gdb -v
```

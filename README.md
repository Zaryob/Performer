# Performer

**Türkçe** · [English](README.en.md)

Performer, Linux süreçlerinde CPU kullanımı, bekleme ve kilit çekişmesini
ölçer. Sonuçları taşınabilir bir `.tgz` paketine yazar; çevrimdışı görüntüleyici
paketi tarayıcıda açar ve iki ölçümü karşılaştırır.

## Görüntüleyici

```sh
docker compose up -d --build --force-recreate viewer
```

[http://127.0.0.1:8080](http://127.0.0.1:8080) adresini açıp bir ölçüm paketi
seçin. Kök [Compose dosyası](compose.yaml) yalnız web görüntüleyicisini
çalıştırır. Paket tarayıcıda okunur; bir sunucuya yüklenmez.

## Ölçüm

Linux makinede Python 3.8+, `bpftrace` ve eBPF yetkileri gerekir:

```sh
sudo ./collector/bin/performer collect --pid PID --duration 30 --profile standard --label baseline --out runs
```

`PID` programın kendi pid'i olmalı; program `sudo ./app` ile başlatıldıysa
`sudo`'nun değil, altındaki sürecin pid'i (`pgrep -f app` veya `pstree -p`).

Donanım sayaçlarını da toplamak için `--pmu basic` ekleyin. Sonuçlar görüntüleyicide
Overview, Threads ve Diff ekranlarında görünür; [sınırlar ve kalite bilgisi](docs/pmu.md).

Başlangıç kontrolü eksik araç veya prob dosyası bulursa ölçüm başlamaz.
Çalışabilen problar varsa diğerlerinin hatası uyarı olarak kaydedilir.
Collector ve görüntüleyici testleri CI'da yalnızca branch'lerde çalışır. Ubuntu 22.04/24.04 için
`performer-collector` `.deb` dosyaları yalnızca `main`'e merge sonrası GitHub ve
GitLab CI çıktılarında üretilir; kurulunca komut `performer` olarak kullanılabilir.

Örnek hedefle test etmek için `make docker-demo`; eBPF olmadan örnek paket
üretmek için `make docker-fake` kullanın. Bu komutların Compose dosyası
[tests/compose.yaml](tests/compose.yaml) içindedir.

## Ayrıntılar

Docker veya eBPF olmadan denemek için [doğrulanmış sentetik paket çiftini](docs/examples/README.md) kullanın. [Yerel doğrulama kaydı](docs/VALIDATION.md) test sonuçlarını ve kalan Linux/eBPF doğrulama boşluklarını ayırır. Sentetik veriler gerçek ölçüm doğruluğu veya overhead kanıtı değildir.

- [Ayrıntılı teknik rehber](docs/guide.en.md)
- [Docker demo ve gereksinimleri](docs/docker-demo.md)
- [Paket biçimi](docs/bundle-format.md)
- [Görüntüleyici geliştirme notları](viewer/README.md)

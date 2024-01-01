# DEIS

## Background

Project to create an automated pipeline with [Docker][doc] and [docker compose][dco] to investigate data from ransomeware leaks.

The project started after a friend asked for help investigating if a data leak contained the friends personal information.

This tool can be used to automate all of (or a selection of) the steps below.

- **D**ownload files from a leek site via [TOR][tor]
- **E**xtract files from .rar, .zip, .tgz and more with the help of [7-zip][7zz]
- **I**ngest into [Elasticsearch][els] with the [Tika][tik] [pipeline][eap]
- **S**earch via [Kibana][kib] and [JupyterLab][jup] notebooks

### Download

Download files automatically from leek sites using [TOR][tor]. I use a [forked][for] version of [aria2-onion-downloader][aod]. Should look at using [docker-aria2-with-webui][aww] if TOR isn't needed or disable TOR in the current package.

Monitor [mayswind/AriaNg][maa] for new releases.

### Extract

Automated extraction of compressed files with a simple container running [7-zip][7zz].

After unpacking the downloaded files a couple of optional (but default) steps are executed.

- Run 7-zip once more on the extracted files.
- Run [readpst][res] on files with the extension [.pst][pst] (Outlook Data File).

### Ingest

Ingest files to search into [Elasticsearch][els] with the [attachment processor][eap] enabled. The processor uses Apache [Tika][tik] to extract text from files.

I've incorporated the [docker-elk][del] repo setup and run Elasticsearch and Kibana but have removed Logstash.

### Search

Search can be done with [Kibana][kib] and a [JupyterLab][jup] notebook. The notebook is my [reuteras/container-notebook][con].

## Requirements

You must increase the RAM that Docker can use to 18 GB or more. Otherwise Elasticsearch will not start if you don't lower the memory specified in the file docker-compose.yml.

## Install and configure

Download the repository from GitHub and change to the new directory.

```bash
git clone https://github.com/reuteras/DEIS.git
cd DEIS
```

Configure DEIS by changing three files:

- Modify passwords in *.env*. If the downloaded files are password protected you must set the **ZIP_PASSWORD**. 
- Add a list of urls (one per line) for files to download to a file in the *urls* directory.
- Copy *deis.cfg.default* to *deis.cfg* and update the settings described in the file.

Setup Elasticsearch and Kibana by running the command below which will start a configuration container and dependent containers.

```bash
docker compose --profile setup up -d
```

Wait for *deis-setup-1* to exit. Tailing the container logs will exit when the container is done after about 45 seconds.

```bash
docker logs deis-setup-1 -f
```

## Run all steps

To run all steps in **DEIS** run.

```bash
docker compose --profile deis up -d
```

You can monitor the download progress by visiting [http://127.0.0.1:8080/](http://127.0.0.1:8080/). You have to enter the value for **RPCSECRET** from the file *.env* (default is **changeme**). After the download is finished the files will be extracted to the directory _extracted_.

After the extraction of is done the ingest process will start. Until it finishes you can see the files *extracted/extensions.txt"* and *extracted/mime.txt* for information about the number of files of different types. Only files with a unique sha256 will be ingested to Elasticsearch.

It is possible to monitor the progress of the process by running the following command (run **make python-bin** first to setup the environment for python).

```bash
.venv/bin/python bin/progress.py
```

Press CTRL-C to exit the progress display.

The following web services are available:

- [http://127.0.0.1:3000/](http://127.0.0.1:3000/) - Gotenberg server 
- [http://127.0.0.1:5601/](http://127.0.0.1:5601/) - Elastic/Kibana
- [http://127.0.0.1:8080/](http://127.0.0.1:8080/) - AriaNg
- [http://127.0.0.1:8081/file/<sha256>](http://127.0.0.1:8081/file/) - Download file based on sha256
- [http://127.0.0.1:8081/convert/<sha256>](http://127.0.0.1:8081/convert/) - Convert file to pdf (if possible) and download file based on sha256
- [http://127.0.0.1:8888/](http://127.0.0.1:8888/) - JupyterLab

## Only run a subset of the steps

### Ingest

If you already have the files available you can skip the download and extraction steps and only ingest the files to Elasticsearch. The files must be in the directory *extracted* or you have to update *deis.cfg*.

```bash
make ingest
./bin/ingest.sh
```

## Search tips

Disable collection by Elastic by opening [http://127.0.0.1:5601/app/management/kibana/settings](http://127.0.0.1:5601/app/management/kibana/settings), click on **Global Settings** and scroll down and click **off** on **Share usage with Elastic**.

Files are added to elastic with timestamp from the filesystem. Search in discovery with absolute time range from *Jan 1, 1970 @ 00:00:00.000* to *now*.

A quick overview of the data is available in the dashboard named **Leaked data**.

To only search a for data already in elastic you can use **docker compose up -d** as start command.

Stop all services with **docker compose --profile deis down**.

If you get a error message about **max_analyzed_offset** open the developer console at [http://127.0.0.1:5601/app/dev_tools#/console](http://127.0.0.1:5601/app/dev_tools#/console) and execute the following command:

```bash
PUT /leakdata-index-*/_settings
{
  "index" : {
    "highlight.max_analyzed_offset" : 2000000000,
  }
}
```

At the same time run the following.

```bash
PUT _cluster/settings 
{
  "persistent": {
    "search.max_async_search_response_size": "50mb"
  }
}
```


## Based on

This project uses several open source tools in combination. A list below and please submit an issue if I have missed any:

- [docker-elk][del]
- [aria2-onion-downloader][aod] which uses [AriaNg][maa]
- Apache [Tika][tik]
- [readpst][res]
- [Tor][tor]
- The whole ELK-stack by [Elastic.co][eco]
- [Jupyterlab][jup]

## TODO

Lots of things :)

  [7zz]: https://www.7-zip.org/
  [aod]: https://github.com/sn0b4ll/aria2-onion-downloader
  [aww]: https://github.com/abcminiuser/docker-aria2-with-webui
  [con]: https://github.com/reuteras/container-notebook
  [del]: https://github.com/deviantony/docker-elk
  [dco]: https://docs.docker.com/compose/
  [doc]: https://www.docker.com/
  [eap]: https://www.elastic.co/guide/en/elasticsearch/reference/current/attachment.html
  [eco]: https://www.elastic.co/
  [els]: https://www.elastic.co/elasticsearch/
  [for]: https://github.com/reece394/aria2-onion-downloader
  [jup]: https://github.com/jupyterlab/jupyterlab
  [kib]: https://www.elastic.co/kibana
  [maa]: https://github.com/mayswind/AriaNg
  [pst]: https://support.microsoft.com/en-au/office/introduction-to-outlook-data-files-pst-and-ost-222eaf92-a995-45d9-bde2-f331f60e2790
  [res]: https://linux.die.net/man/1/readpst
  [tik]: https://tika.apache.org/
  [tor]: https://www.torproject.org/


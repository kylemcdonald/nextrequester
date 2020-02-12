import asyncio
import math
import os
import re

import aiohttp
import requests
from bs4 import BeautifulSoup as bs

from util.auth import login_session, get_csrf_token
from util.config import get_download_dir
from util.constants import DOCUMENTS_URL, BASE_URL, REQUESTS_URL
from util.file import build_filename, parse_document_id


def download_file(session, url, filename, sub_dir=None):
    response = session.get(url)
    print('{} : {}'.format(url, response.status_code))
    if sub_dir:
        dl_dir = os.path.join(get_download_dir(), sub_dir)
    else:
        dl_dir = get_download_dir()
    dl_path = os.path.join(dl_dir, filename.replace('/', '-').replace(':', '-'))

    with open(dl_path, 'wb') as file:
        file.write(response.content)
        print('Saved {} to {}...'.format(url, dl_path))
        file.close()


async def afetch(session, url):
    async with session.get(url) as response:
        print('fetching {}...'.format(url))
        text = await response.text()

        return {'text': text, 'url': url}


async def adownload_file(session, url, filename, sub_dir=None):
    # fetch file
    async with session.get(url) as response:
        print('{} : {}'.format(url, response.status))
        if sub_dir:
            dl_dir = os.path.join(get_download_dir(), sub_dir)
        else:
            dl_dir = get_download_dir()
        dl_path = os.path.join(dl_dir, filename.replace('/', '-').replace(':', '-'))
        # write file
        _file = await response.read()

        with open(dl_path, 'wb') as file:
            file.write(_file)
            print('Saved {} to {}...'.format(url, dl_path))


def download_all_request_files(user, req_id):

    rsession = requests.session()
    login_session(session=rsession, user=user)
    csrf_token = get_csrf_token(rsession)

    rsession.headers.update({
        'x-csrf-token': csrf_token,
        'user-agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/80.0.3987.87 Safari/537.36',
        'accept-encoding': 'gzip, deflate, br',
        'x-requested-with': 'XMLHttpRequest',
        'Connection': 'close',
    })

    request_response = rsession.get('{}/requests/{}'.format(BASE_URL, req_id))
    request_content = request_response.content
    request_id = re.search('request_id: "([0-9]+?)"', str(request_content)).group(1)

    docs_response = rsession.get('{}/documents/batch?request_id={}&amp;state=requester&amp;_=1581458452674",'.format(BASE_URL, request_id))
    dr_text = docs_response.text
    page_link_matches = re.findall('&page=([0-9]*)', dr_text)
    page_ints = [int(p) for p in page_link_matches]
    page_ints.sort()
    total_pages = page_ints[len(page_ints) - 1]

    for page in range(1, total_pages + 1):
        link_data = []
        print('Scraping page {} of {}...'.format(page, total_pages))
        page_response = rsession.get(
            '{}/documents/batch?request_id={}&state=requester&page={}'.format(BASE_URL, request_id, str(page)))
        page_text = page_response.text
        doc_matches = set(re.findall('/documents/([0-9]*)/download[^>]*>([^<]*)', page_text))
        for match in doc_matches:
            link_data.append({
                'url': '{}/documents/{}/download'.format(BASE_URL, match[0]),
                'filename': match[1],
            })

        for link in link_data:
            if link['url']:
                download_file(
                    session=rsession,
                    url=link['url'],
                    filename=link['filename'],
                    sub_dir=req_id,
                )

    rsession.close()


async def download_all_documents(rsession):
    """Download all the files found at /documents. Can be run without auth, but will only include
    all files with auth.

    :param rsession: requests.Session() instance (with or without authentication).
        Note: This session is used for synchronous functionality, whereas asession is used for
            asynchronous functionality. Cookies and headers are copied from rsession to asession.
    """
    # fetch the first page of the documents list to calculate the number of pages.
    count_response = rsession.get('{}?documents_smart_listing[per_page]=100'.format(DOCUMENTS_URL))

    # get the total number of results...
    results_count = int(bs(count_response.content, 'html.parser').find(class_='count').text)
    # and calculate number of pages when 100 results are shown per page.
    page_count = math.ceil(results_count / 100)
    # build a list of urls from the page numbers and other parameters
    per_page_param = 'documents_smart_listing[per_page]={}'.format(100)
    sort_param = 'documents_smart_listing[sort][count]=desc'
    params = '&'.join([per_page_param, sort_param])
    page_params = ['documents_smart_listing[page]={}'.format(p) for p in range(1, page_count + 1)]
    list_page_urls = ['{}/documents?{}&{}'.format(BASE_URL, pp, params) for pp in page_params]

    # use asession to asynchronously fetch each of the urls in the list
    asession = aiohttp.ClientSession(
        headers=rsession.headers,
        cookies=rsession.cookies,
    )
    dl_data = []

    async with asession:
        # fetch each page of the documents list
        list_page_responses = await asyncio.gather(*[afetch(asession, u) for u in list_page_urls])
        doc_page_urls = set()

        for list_page_response in list_page_responses:
            response_text = list_page_response['text']
            soup = bs(response_text, 'html.parser')
            doc_links = soup.find_all(class_='document published')
            print('found {} links on page.'.format(len(doc_links)))
            doc_page_urls.update(['{}{}'.format(BASE_URL, link['href']) for link in doc_links])

        # fetch each of the document pages to get the full filename (since the list tends to cut them off)
        doc_page_responses = await asyncio.gather(*[afetch(asession, d) for d in doc_page_urls])
        for doc_page_response in doc_page_responses:
            doc_page = bs(doc_page_response['text'], 'html.parser')
            page_header = doc_page.find(class_='document-header')
            if page_header:
                filename = build_filename(page_header, doc_page_response['url'])
            else:
                filename = 'missing filename {}'.format(parse_document_id(doc_page_response['url']))

            if not filename:
                print('failed to parse filename for file at {}'.format(doc_page_response['url']))
            dl_data.append({
                'filename': filename,
                'url': doc_page_response['url'],
            })

        # download each file and save it to the appropriate location
        await asyncio.gather(
            *[adownload_file(asession, d['url'], d['filename'], sub_dir='documents') for d in dl_data if d['url']])


async def print_all_requests(rsession, outfile='requests.csv'):
    # fetch the first page of the documents list to calculate the number of pages.
    count_response = rsession.get(REQUESTS_URL)
    soup = bs(count_response.content, 'html.parser')

    # get the total number of results...
    results_count = int(soup.find(class_='count').text)
    # and calculate number of pages based on number shown per page (25 by default).
    # for some reason, passing the per_page param doesn't work on /requests like it does on /documents
    per_page = 25  # TODO: figure out a way to do 100 per page
    page_count = math.ceil(results_count / per_page)
    # build a list of urls from the page numbers and other parameters
    page_param = ['requests_smart_listing[page]={}'.format(p) for p in range(1, page_count + 1)]
    list_page_urls = ['{}/requests?{}'.format(BASE_URL, pp) for pp in page_param]

    # use asession to asynchronously fetch each of the urls in the list
    asession = aiohttp.ClientSession(
        headers=rsession.headers,
        cookies=rsession.cookies,
    )

    async with asession:
        # fetch each page of the requests list
        list_page_responses = await asyncio.gather(*[afetch(asession, u) for u in list_page_urls])

    for list_page_response in list_page_responses:
        soup = bs(list_page_response['text'], 'html.parser')
        rows = soup.find_all(class_='demo-data-false')
        for row in rows:
            link = row.find('a')
            url = row.find('a').get_attribute_list('href')[0]
            r_id = link.text.replace(link.find('span').text, '').strip()
            cols = row.find_all('td')
            status = 'open' if len(row.find_all(class_='fa-folder-open')) > 0 else 'closed'
            date = cols[len(cols) - 4].text.strip()
            desc = cols[len(cols) - 3].find('a').text.replace(
                cols[len(cols) - 3].find('span').text, ''
            ).replace('\n', ' ').replace('\t', ' ').replace('"', '\'').strip()
            depts = cols[len(cols) - 2].text.strip()
            pocs = cols[len(cols) - 1].text.strip()

            print(BASE_URL + url)

            line = '{url}\t{id}\t{status}\t{date}\t"{desc}"\t"{depts}"\t"{pocs}"'.format(
                url=BASE_URL + url,
                id=r_id,
                status=status,
                date=date,
                desc=desc,
                depts=depts,
                pocs=pocs,
            )

            print(line, file=open(outfile, 'a'))

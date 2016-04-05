#!/usr/bin/python
# -*- coding: UTF-8 -*-

"""
A python/CLI API for some StartCom StartSSL functions.

Website: https://github.com/freddy36/StartSSL_API

Dependencies:
  apt-get install python-httplib2 python-pyasn1 python3-pyasn1-modules

Copyright (c) 2014, Frederik Kriewitz <frederik@kriewitz.eu>.

This library is free software; you can redistribute it and/or
modify it under the terms of the GNU Lesser General Public
License as published by the Free Software Foundation; either
version 2.1 of the License, or (at your option) any later version.

This library is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the GNU
Lesser General Public License for more details.

You should have received a copy of the GNU Lesser General Public
License along with this library; if not, write to the Free Software
Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston,
MA 02110-1301 USA
"""

from __future__ import print_function
try:
    from urllib.parse import urlencode  # python 3
except ImportError:
    from urllib import urlencode  # python 2

__version__ = "1.05"

import argparse
import httplib2
import re
import datetime
import os
import sys
import traceback

import base64
import pyasn1
import pyasn1.codec.der.decoder
import pyasn1_modules.rfc2314
import pyasn1_modules.rfc2459

import zipfile
import io

import json
import uuid


class CSR:
    """
    Parses CSRs
    """
    id_PKCS9_extensionRequest = pyasn1.type.univ.ObjectIdentifier('1.2.840.113549.1.9.14')


    def __init__(self, pem_csr):
        if 'read' in dir(pem_csr):
            pem_csr = pem_csr.read()

        self.pem = pem_csr
        self.__parse_pem()

    def __parse_pem(self):
        """
        Parses a PEM encoded CSR to asn1
        """
        matches = re.search(
            "-----BEGIN CERTIFICATE REQUEST-----([A-Za-z0-9+/=\n\r\t ]*)-----END CERTIFICATE REQUEST-----", self.pem)
        if not matches:
            raise ValueError("Not a valid PEM CSR")

        csr_b64 = matches.group(1)
        csr_bin = base64.b64decode(csr_b64)
        self.asn1, _ = pyasn1.codec.der.decoder.decode(csr_bin, asn1Spec=pyasn1_modules.rfc2314.CertificationRequest())

    def get_pem(self):
        """
        Returns the PEM encoded CSR.
        """
        return self.pem

    def get_common_name(self):
        """
        Returns the common-name
        """
        subject_rdn_sequence = self.asn1.getComponentByName('certificationRequestInfo').getComponentByName('subject')[0]
        for subject in subject_rdn_sequence:
            name = subject[0]
            oid = name.getComponentByName('type')
            value = name.getComponentByName('value')
            if oid == pyasn1_modules.rfc2459.id_at_commonName:
                value = pyasn1.codec.der.decoder.decode(value, asn1Spec=pyasn1_modules.rfc2459.DirectoryString())[0]
                return str(value.getComponent())

    def get_subject_alt_names(self, types=None):
        """
        Yields (type, value) tupels for each SubjectAltName.
        Types can be specified to filter limit the result to specific types.
        """
        for attribute_type, attribute_value in self.asn1.getComponentByName(
                'certificationRequestInfo').getComponentByName('attributes'):
            if attribute_type != self.id_PKCS9_extensionRequest:  # we're only interested in the extension request part
                continue

            extensions, _ = pyasn1.codec.der.decoder.decode(attribute_value[0],
                                                            asn1Spec=pyasn1_modules.rfc2459.Extensions())
            for extension in extensions:
                oid = extension.getComponentByName('extnID')
                if oid != pyasn1_modules.rfc2459.id_ce_subjectAltName:  # we're only interested in the subject alternative name
                    continue

                subject_alt_names_raw = pyasn1.codec.der.decoder.decode(extension.getComponentByName('extnValue'),
                                                                        asn1Spec=pyasn1.type.univ.OctetString())[0]
                subject_alt_names = pyasn1.codec.der.decoder.decode(subject_alt_names_raw,
                                                                    asn1Spec=pyasn1_modules.rfc2459.SubjectAltName())[0]
                for general_name in subject_alt_names:
                    subject_alt_name_type = general_name.getName()
                    subject_alt_name_value = general_name.getComponent()
                    if types and subject_alt_name_type not in types:  # skip unwanted types
                        continue
                    yield subject_alt_name_type, str(subject_alt_name_value)


class API(object):
    """
    Provides a python API for some StartCOM StartSSL functions
    """
    STARTCOM_CA = "/etc/ssl/certs/StartCom_Certification_Authority.pem"
    STARTSSL_BASEURI = "https://startssl.com"
    STARTSSL_AUTHURI = "https://auth.startssl.com"
    STARTSSL_TOOLBOXURI = "https://startssl.com/ToolBox"
    STARTSSL_GETDOMAINSURI = "https://startssl.com/ControlPanel/AjaxRequestGetAllDomainValis"
    STARTSSL_GETEMAILSURI = "https://startssl.com/ControlPanel/AjaxRequestGetAllEmailValis"
    STARTSSL_SUBMITCSR = "https://startssl.com/Certificates/ssl"

    RETRIEVE_CERTIFICATE_LIST = re.compile(
        '<tr style="text-align:center;">\s+<td style="vertical-align:middle;">(?P<order_number>\d+)</td>\s+<td align="left" style="vertical-align:middle;" title="(?P<name>.+?)">.+?</td>\s+<td align="left" style="vertical-align:middle;">(?P<product>[\w ]+?)</td>\s+<td style="vertical-align:middle;">\s*(?:<span>)?(?P<issuance_date_year>\d{4})?-?(?P<issuance_date_month>\d{2})?-?(?P<issuance_date_day>\d{2})?(?:</span><br /><span>)?(?P<expiry_date_year>\d{4})?-?(?P<expiry_date_month>\d{2})?-?(?P<expiry_date_day>\d{2})?(?:</span>)?\s*</td>\s+<td style="vertical-align:middle;">\s+(?P<status>.+?)<!--.*?-->\s+</td>\s+<td align="center" style="vertical-align:middle;">\s*(?s)(?P<actions_code>.*?)\s*</td>\s*</tr>',
        re.UNICODE)
    RETRIEVE_CERTIFICATE_LIST_ACCTION_ID = re.compile('orderId=(?P<orderId>\w+)')
    REQUEST_CERTIFICATE_CSR_ID = re.compile(
        'x_third_step_certs\(\\\\\'(?P<type>\w+?)\\\\\',\\\\\'(?P<csr_id>\d+?)\\\\\',\\\\\'(?P<unknown>.*?)\\\\\',showCertsWizard\);')
    REQUEST_CERTIFICATE_READY_CN = re.compile(
        '<li>The common name of this certificate will be set to <b><i>(?P<cn>.+?)</i></b>.</li>')
    REQUEST_CERTIFICATE_READY_DOMAINS = re.compile('<li><b><i>(?P<domain>.+?)</i></b></li>')
    REQUEST_CERTIFICATE_CERT = re.compile('<textarea.*?>(?P<certificate>.*?)</textarea>')
    VALIDATED_RESSOURCES = re.compile('<td nowrap>(?P<resource>.+?)</td><td nowrap> <img src="/img/yes-sm.png"></td>')
    CERTIFICATE_PROFILES = {'smime': "S/MIME", 'server': "Server", 'xmpp': "XMPP", 'code': "Object"}

    def __init__(self, ca_certs=STARTCOM_CA, user_agent=None):
        """
        Init the StartSSL API.

        :param ca_certs: PEM encoded CA certificate file to authenticate the server
        """
        self.h = httplib2.Http(ca_certs=ca_certs)
        self.h.follow_redirects = False
        self.user_agent = user_agent
        self.validated_emails = None
        self.validated_domains = None
        self.authenticated = False
        self.cookies = None

    # noinspection PyShadowingNames
    def __request(self, *args, **kwargs):
        """
        Wrapper for HTTP requests
        """
        # make sure headers exist
        if "headers" not in kwargs:
            kwargs['headers'] = {}

        if self.user_agent:
            kwargs['headers']['User-Agent'] = self.user_agent

        # add (overwrite) cookies
        if self.cookies:
            kwargs['headers']['Cookie'] = self.cookies

        # urlencode body if list
        if "body" in kwargs and type(kwargs['body']) is list:
            kwargs['body'] = urlencode(kwargs['body'])

        # add Content-Type: urlencoded if method is POST and content type is unset
        if "method" in kwargs and kwargs['method'] == "POST" and "Content-Type" not in kwargs['headers']:
            kwargs['headers']['Content-Type'] = "application/x-www-form-urlencoded"

        resp, content = self.h.request(*args, **kwargs)
        if resp.get("content-type", None) == 'text/html; charset=utf-8':
            content = content.decode('utf-8')

        return resp, content

    # noinspection PyShadowingNames
    def authenticate(self, cert, key):
        """
        Use the cert/key to authenticate the session.

        :param cert: path to pem encoded client certificate
        :param key: path to pem encoded client key
        :return: True on success
        """
        self.h.add_certificate(key, cert, '')
        resp, content = self.__request(self.STARTSSL_AUTHURI, method="GET")
        assert resp.status == 302, resp
        assert resp["location"].startswith("https://Startssl.com/ControlPanel"), resp
        assert "set-cookie" in resp, resp
        assert resp["set-cookie"].startswith("MyStartSSLCookie="), resp["set-cookie"]
        self.cookies = resp["set-cookie"]
        self.authenticated = True

        return self.authenticated

    def get_validated_resources(self, force_update=False):
        """
        Returns validated resources (emails/domains) which can be used in certificate requests.
        By default the data is only updated during the initial call. After that the cached data will be returned.

        :param force_update: Setting this to True will refresh the cache
        :return: [validated_emails], [self.validated_domains]
        """
        assert self.authenticated, "not authenticated"
        if self.validated_emails is not None and self.validated_domains is not None and not force_update:
            return self.validated_emails, self.validated_domains

        self.validated_emails = []
        self.validated_domains = []

        body = [('app', 12)]
        resp, content = self.__request(self.STARTSSL_GETDOMAINSURI + '?cacheKey=' + str(uuid.uuid4()), method="GET", body=body)
        assert resp.status == 200

        parsed_domains = json.loads(content.decode())

        for domain in parsed_domains:
            self.validated_domains.append(domain['Domain'])

        resp, content = self.__request(self.STARTSSL_GETEMAILSURI + '?cacheKey=' + str(uuid.uuid4()), method="GET", body=body)
        assert resp.status == 200

        parsed_emails = json.loads(content.decode())

        for email in parsed_emails:
            self.validated_emails.append(email['Email'])

        return self.validated_emails, self.validated_domains

    def is_validated_domain(self, domain):
        """Check the validation status of a (sub)domain

        :param domain: (sub)domain to check
        :return: the validated (parent) domain or False
        """
        self.get_validated_resources()

        # noinspection PyTypeChecker
        for validated_domain in self.validated_domains:
            if domain.endswith(validated_domain):
                return validated_domain
        return False

    def get_certificates_list(self):
        """
        Returns the available signed certificates.

        Each certificate entry (dict) has the following keys:
        'id', 'order_number', 'name', 'class', 'profile', 'product', 'status',
        'issuance_date' (datetime), 'issuance_date_day', 'issuance_date_year', 'issuance_date_month'
        'expiry_date' (datetime), 'expiry_date_day', 'expiry_date_year', 'expiry_date_month'

        :return: a list of certificate dicts
        """
        hasNextPage = True
        pageindex = 0
        while hasNextPage:
            resp, content = self.__request(self.STARTSSL_BASEURI+"/CertList?pageindex="+str(pageindex), method="GET")
            assert resp.status == 200, resp
            assert "Certificate List<!--Cert List-->" in content, content

            items = self.RETRIEVE_CERTIFICATE_LIST.finditer(content)
            for item in items:
                cert = item.groupdict()

                # convert Issuance Date
                if cert['issuance_date_year'] != None:
                    cert['issuance_date'] = datetime.date(int(cert['issuance_date_year']), int(cert['issuance_date_month']), int(cert['issuance_date_day']))
                    cert['expiry_date'] = datetime.date(int(cert['expiry_date_year']), int(cert['expiry_date_month']), int(cert['expiry_date_day']))
                else:
                    cert['issuance_date'] = None
                    cert['expiry_date'] = None

                # convert to integer
                cert['order_number'] = int(cert['order_number'])

                # convert profile description to profile identifier

                if cert['product'].endswith("SSL"):
                    cert['profile'] = "server"
                elif cert['product'].endswith("Client"):
                    cert['profile'] = "client"
                elif cert['product'].endswith("Code Signing"):
                    cert['profile'] = "object"
                else:
                    cert['profile'] = None

                if cert['product'].startswith("Class"):
                    cert['class'] = int(cert['product'][6])
                else:
                    cert['class'] = None

                item = self.RETRIEVE_CERTIFICATE_LIST_ACCTION_ID.search(cert['actions_code'])
                if item != None:
                    cert['id'] = item.group('orderId')
                else:
                    cert['id'] = None
                del cert['actions_code']

                """
                # set retrieved state depending on the background color
                if cert['color'] == "FFFFFF":
                    cert['retrieved'] = True
                else:  # if color = rgb(201, 255, 196)
                    cert['retrieved'] = False
                del cert['color']
                """
                yield cert

            hasNextPage = ">Next page</a>" in content
            pageindex += 1


    def get_certificate_zip(self, certificate_id):
        """
        Returns a the certificate zip bundle.

        Use get_certificates_list() to find the id or use the certificate_id returned by submit_certificate_request()

        :param certificate_id: StartSSL internal id of the certificate
        :return: ZIP file as bytes
        """

        resp, content = self.__request(self.STARTSSL_BASEURI+"/CertList/DownLoadCert?orderId="+str(certificate_id), method="GET")
        assert resp.status == 200, resp
        assert resp['content-type'] == 'application/octet-stream', resp
        assert resp['content-disposition'].startswith('attachment; filename='), resp

        attachment_filename = resp['content-disposition'][len('attachment; filename='):]
        return attachment_filename, content


    def get_certificate(self, certificate_id):
        """
        Returns a certificate, it's basename (Common Name) and the corresponding intermediate certificate.

        Use get_certificates_list() to find the id or use the certificate_id returned by submit_certificate_request()

        :param certificate_id: StartSSL internal id of the certificate
        :return: basename (common name), PEM encoded certificate
        """

        attachment_filename, zip_file = self.get_certificate_zip(certificate_id)
        assert attachment_filename[-4:] == ".zip", attachment_filename
        basename = attachment_filename[0:-4]
        zf_main = zipfile.ZipFile(io.BytesIO(zip_file), "r")
        assert zf_main.testzip() == None, "invalid zip file"

        if "OtherServer.zip" in zf_main.namelist(): # Server
            zf_server = zipfile.ZipFile(io.BytesIO(zf_main.read("OtherServer.zip")), "r")
            intermediate_filename = "1_Intermediate.crt"
            server_filename = "2_"+basename+".crt"

            assert len(zf_server.namelist()) == 3, zf_server.namelist()
            assert intermediate_filename in zf_server.namelist(), zf_server.namelist()
            assert server_filename in zf_server.namelist(), zf_server.namelist()

            intermediate_cert = zf_server.read(intermediate_filename).decode("ascii")
            cert = zf_server.read(server_filename).decode("ascii")
        elif len(zf_main.namelist()) == 2: # Client + Object
            intermediate_filename = "1_Intermediate.crt"
            cert_filename = zf_main.namelist()[1]
            assert intermediate_filename != cert_filename, zf_main.namelist()

            assert intermediate_filename in zf_main.namelist(), zf_main.namelist()
            assert cert_filename in zf_main.namelist(), zf_main.namelist()

            intermediate_cert = zf_main.read(intermediate_filename).decode("ascii")
            cert = zf_main.read(cert_filename).decode("ascii")
        else:
            raise ValueError("unexpected zip content: "+str(zf_main.namelist()))

        assert "-----BEGIN CERTIFICATE-----" in intermediate_cert, "no BEGIN CERTIFICATE"
        assert "-----END CERTIFICATE-----" in intermediate_cert, "no END CERTIFICATE"

        assert "-----BEGIN CERTIFICATE-----" in cert, "no BEGIN CERTIFICATE"
        assert "-----END CERTIFICATE-----" in cert, "no END CERTIFICATE"

        return basename, cert, intermediate_cert


    def submit_certificate_request(self, profile, csr):
        """
        Submits a CSR.
        The common name and SubjectAltNames are extracted from the CSR.

        :param profile: the StartSSL profile which should be used (server or xmpp)
        :param csr: CSR instance
        :return: certificate_id (StartSSL internal id of the certificate),
                 common_name,
                 domains (dNSName SubjectAltNames),
                 certificate (PEM encoded certificate or None if manual approval by StartSSL is required
        """

        assert profile in self.CERTIFICATE_PROFILES, "unknown profile"

        self.get_validated_resources()

        if profile in ['server', 'xmpp']:
            csr_cn = csr.get_common_name()
            subjects = [csr_cn]
            for t, v in csr.get_subject_alt_names(types=['dNSName']):
                if v not in subjects:
                    subjects.append(v)

            assert len(subjects) > 0, "no subjects found"

            subjects_direct = []
            subjects_subdomain = []
            validated_domain_first = None
            for subject in subjects:
                validated_domain = self.is_validated_domain(subject)
                if validated_domain:
                    if validated_domain not in subjects_direct:
                        subjects_direct.append(validated_domain)
                    if subject != validated_domain:
                        subjects_subdomain.append(subject)

                    if not validated_domain_first:
                        validated_domain_first = validated_domain
                else:
                    raise ValueError("Missing domain validations for %s." % subject)

            assert len(subjects_direct) > 0, "no direct subjects identified."

            # submit CSR
            body = [('domains', "".join(subjects)), ('rbcsr', 'scsr'), ('areaCSR', csr.get_pem()), ('hidchekcer', '1'), ('__EVENTTARGET', 'btnSubmit')]
            resp, content = self.__request(self.STARTSSL_SUBMITCSR, method="POST", body=body)
            assert resp.status == 302, "CSR req is not redirecting"
            resp, content = self.__request(self.STARTSSL_BASEURI + resp['location'], method="GET")
            assert resp.status == 200, "second_step_certs bad status"

if __name__ == "__main__":
    config_files = ['/etc/startssl.conf', 'startssl.conf']
    parser = argparse.ArgumentParser(prog="StartSSL_API", description="A CLI for some StartSSL functions.", fromfile_prefix_chars='@', epilog="Arguments are also read from the following config files: %s (use @/path/to/file to specify more files)" % ", ".join(config_files))
    parser.add_argument('--ca_certs', help='CA certificate file (PEM) to authenticate the server (default: %(default)s',
                        required=False, default=API.STARTCOM_CA, type=argparse.FileType('r'))
    parser.add_argument('--client_crt', help='Client certificate file (PEM)', required=True,
                        type=argparse.FileType('r'))
    parser.add_argument('--client_key', help='Client key file (PEM)', required=True, type=argparse.FileType('r'))
    parser.add_argument('--user_agent', help='HTTP User Agent to use', default="StartSSL_API/%s (+https://github.com/freddy36/StartSSL_API)" % __version__, type=str)
    parser.add_argument('--version', action='version', version='%(prog)s ' + __version__)

    subparsers = parser.add_subparsers(title='subcommands',
                                       description='valid subcommands, run them with -h for more details')
    parser_csr = subparsers.add_parser('csr', help='Submit a CSR')
    parser_csr.set_defaults(cmd="csr")
    parser_csr.add_argument('--profile', choices=['server', 'xmpp'], help='StartSSL profile', default="server",
                            type=str)
    parser_csr.add_argument('csr_files', nargs=argparse.REMAINDER, type=argparse.FileType('r'), help="CSR files (PEM)")
    parser_certs = subparsers.add_parser('certs', help='Retrieves signed certificates',
                                         description='Retrieves certificates from StartSSL. By default all available certificates are listed.')
    parser_certs.set_defaults(cmd="certs")
    parser_certs.add_argument('--store', action="append", choices=['all', 'new', 'missing'], default=[],
                              help="Retrieve all (replace any existing), new (never downloaded/feature currently broken), missing (target file missing) certificates")
    parser_certs.add_argument('--list_format',
                              default="Order Number: {order_number}, {name}, Profile: {profile}, Class: {class}, Product: {product}, Status: {status}, Issuance date: {issuance_date}, Expiry date: {expiry_date}, id: {id}",
                              type=str, help="default: %(default)s")
    parser_certs.add_argument('--filename_format', default="{name}.crt", type=str,
                              help="default: %(default)s, use - for stdout")
    parser_certs.add_argument('certificates', nargs=argparse.REMAINDER,
                              help="Retrieve specific certificates by name or id", type=str)
    args_src = []
    for config_file in config_files:
        if os.path.exists(config_file):
            args_src.append("@"+config_file)
    args_src += sys.argv[1:]
    args = parser.parse_args(args=args_src)

    api = API(ca_certs=args.ca_certs.name, user_agent=args.user_agent)
    api.authenticate(args.client_crt.name, args.client_key.name)
    if args.cmd == "certs":
        certs = api.get_certificates_list()
        if not args.store and not args.certificates:
            for cert in certs:
                print(args.list_format.format(**cert))
        else:
            for cert in certs:
                filename = args.filename_format.format(**cert)
                if (("all" in args.store) or
                        ("new" in args.store and not cert['retrieved']) or
                        ("missing" in args.store and not os.path.exists(filename)) or
                        (cert['name'] in args.certificates) or
                        (str(cert['order_number']) in args.certificates)):
                    basename, cert, intermediate_cert = api.get_certificate(cert['id'])
                    if filename == "-":
                        print(cert)
                    else:
                        f = open(filename, 'w')
                        f.write(cert)
                        f.close()
                        print("stored", filename)
    elif args.cmd == "csr":
        for csr_file in args.csr_files:
            try:
                print("Submitting %s" % csr_file.name)
                csr = CSR(csr_file)
                api.submit_certificate_request(profile=args.profile, csr=csr)

                print("Submission successful;")

            except ValueError as e:
                print("Submission failed:", e)
            except Exception as e:
                print("Submission failed:")
                print(traceback.format_exc(), file=sys.stderr)

    sys.exit(0)

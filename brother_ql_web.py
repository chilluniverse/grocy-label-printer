#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""
This is a web service to print labels on Brother QL label printers.
"""

import sys, logging, random, json, argparse
from io import BytesIO
import os

from bottle import run, route, get, post, response, request, jinja2_view as view, static_file, redirect
from PIL import Image, ImageDraw, ImageFont

from barcode import Code128
from barcode.writer import ImageWriter
from PyPDF2 import PdfReader, PdfWriter
import cups

from brother_ql.devicedependent import models, label_type_specs, label_sizes
from brother_ql.devicedependent import ENDLESS_LABEL, DIE_CUT_LABEL, ROUND_DIE_CUT_LABEL
from brother_ql import BrotherQLRaster, create_label
from brother_ql.backends import backend_factory, guess_backend

from font_helpers import get_fonts

logger = logging.getLogger(__name__)

LABEL_SIZES = [ (name, label_type_specs[name]['name']) for name in label_sizes]

try:
    with open('config.json', encoding='utf-8') as fh:
        CONFIG = json.load(fh)
except FileNotFoundError as e:
    with open('config.example.json', encoding='utf-8') as fh:
        CONFIG = json.load(fh)

#> Default Values
default_label_size = "57x32"

@route('/')
def index():
    redirect('/labeldesigner')

@route('/static/<filename:path>')
def serve_static(filename):
    return static_file(filename, root='./static')

@route('/labeldesigner')
@view('labeldesigner.jinja2')
def labeldesigner():
    font_family_names = sorted(list(FONTS.keys()))
    return {'font_family_names': font_family_names,
            'fonts': FONTS,
            'label_sizes': LABEL_SIZES,
            'website': CONFIG['WEBSITE'],
            'label': CONFIG['LABEL']}

def get_label_context(request):
    """ might raise LookupError() """
    d = request.params.decode() # UTF-8 decoded form data

    font_family = d.get('font_family').rpartition('(')[0].strip()
    font_style  = d.get('font_family').rpartition('(')[2].rstrip(')')
    
    context = {
      'grocycode': d.get('grocycode', None),
      'product': d.get('product', None),
      'duedate': d.get('duedate', None),
      'label_width': int(d.get('label_size', default_label_size).rpartition('x')[0].rstrip()),
      'label_height': int(d.get('label_size', default_label_size).rpartition('x')[2].rstrip()),
      'dpi': d.get('dpi',300),
      'font_family':   font_family,
      'font_style':    font_style
    }
    
    
    def get_font_path(font_family_name, font_style_name):
        try:
            if font_family_name is None or font_style_name is None:
                font_family_name = CONFIG['LABEL']['DEFAULT_FONTS']['family']
                font_style_name =  CONFIG['LABEL']['DEFAULT_FONTS']['style']
            font_path = FONTS[font_family_name][font_style_name]
        except KeyError:
            raise LookupError("Couln't find the font & style")
        return font_path

    context['font_path'] = get_font_path(context['font_family'], context['font_style'])

    return context

def create_label_im(text, **kwargs):
    label_type = kwargs['kind']
    im_font = ImageFont.truetype(kwargs['font_path'], kwargs['font_size'])
    im = Image.new('L', (20, 20), 'white')
    draw = ImageDraw.Draw(im)
    # workaround for a bug in multiline_textsize()
    # when there are empty lines in the text:
    lines = []
    for line in text.split('\n'):
        if line == '': line = ' '
        lines.append(line)
    text = '\n'.join(lines)
    #linesize = im_font.getbbox(text)
    textsize = draw.multiline_textbbox(xy=(1000,1000),text=text, font=im_font)
    width, height = kwargs['width'], kwargs['height']
    if kwargs['orientation'] == 'standard':
        if label_type in (ENDLESS_LABEL,):
            height = textsize[1] + kwargs['margin_top'] + kwargs['margin_bottom']
    elif kwargs['orientation'] == 'rotated':
        if label_type in (ENDLESS_LABEL,):
            width = textsize[0] + kwargs['margin_left'] + kwargs['margin_right']
    im = Image.new('RGB', (width, height), 'white')
    draw = ImageDraw.Draw(im)
    if kwargs['orientation'] == 'standard':
        if label_type in (DIE_CUT_LABEL, ROUND_DIE_CUT_LABEL):
            vertical_offset  = (height - textsize[1])//2
            vertical_offset += (kwargs['margin_top'] - kwargs['margin_bottom'])//2
        else:
            vertical_offset = kwargs['margin_top']
        horizontal_offset = max((width - textsize[0])//2, 0)
    elif kwargs['orientation'] == 'rotated':
        vertical_offset  = (height - textsize[1])//2
        vertical_offset += (kwargs['margin_top'] - kwargs['margin_bottom'])//2
        if label_type in (DIE_CUT_LABEL, ROUND_DIE_CUT_LABEL):
            horizontal_offset = max((width - textsize[0])//2, 0)
        else:
            horizontal_offset = kwargs['margin_left']
    offset = horizontal_offset, vertical_offset
    draw.multiline_text(offset, text, kwargs['fill_color'], font=im_font, align=kwargs['align'])
    return im

def create_label_grocy(kwargs):
    """
    Erstellt ein Label mit Text und einem Code-128 Barcode.
    
    :param text_lines: Liste mit Textzeilen (max. 2 Zeilen)
    :param barcode_data: Daten für den Code-128 Barcode
    :param label_size_mm: Maße des Labels in Millimetern (Breite, Höhe)
    :param dpi: Druckauflösung (dots per inch)
    :return: PIL-Image des Labels
    """

    text_lines = [kwargs['product'],kwargs['duedate']]
    print(text_lines)
    barcode_data = kwargs['grocycode']
    label_size_mm = (kwargs['label_width'],kwargs['label_height'])
    dpi = kwargs['dpi']
    
    # Berechnung der Pixel basierend auf DPI
    label_size_px = (int(label_size_mm[0] / 25.4 * dpi), int(label_size_mm[1] / 25.4 * dpi))

    # Erstelle das Label-Bild
    label_image = Image.new("RGB", label_size_px, "white")
    draw = ImageDraw.Draw(label_image)

    desired_font_height_mm = 6
    # Schriftart laden (Systemschrift oder Standardschrift)
    try:
        font_size = int(desired_font_height_mm / 25.4 * dpi)  # Schriftgröße: 8 mm (entspricht ~22.7 pt bei 300 DPI)
        font = ImageFont.truetype(kwargs['font_path'], font_size)
    except IOError:
        font = ImageFont.load_default()


    # Text hinzufügen (1. und 2. Zeile)
    y_offset = int(1.5 / 25.4 * dpi)  # Oberer Rand: 3 mm
    for line in text_lines[:2]:  # Maximal 2 Zeilen
        text_width = draw.textlength(line, font=font)
        text_height = font_size
        x_offset = (label_size_px[0] - text_width) // 2  # Zentrierung
        draw.text((x_offset, y_offset), line, fill="black", font=font)
        y_offset += text_height + int(3 / 25.4 * dpi)  # Abstand zwischen den Zeilen

    # Barcode hinzufügen
    barcode_class = Code128(barcode_data, writer=ImageWriter())
    barcode_options = {
        "module_width": 0.3,  # Breite eines Moduls in mm
        "module_height": 15,  # Höhe des Barcodes: 15 mm
        "quiet_zone": 0,  # Abstand links und rechts
        "font_size": 1,  # Keine Schrift unter dem Barcode
        "text_distance": 0,  # Abstand zwischen Barcode und Text
    }
    barcode_img_buffer = BytesIO()
    barcode_class.write(barcode_img_buffer, options=barcode_options)
    barcode_img_buffer.seek(0)

    # Barcode als Bild laden und skalieren
    barcode_pil_img = Image.open(barcode_img_buffer)
    barcode_width, barcode_height = barcode_pil_img.size
    scale_factor = label_size_px[0] / barcode_width  # Breite anpassen
    barcode_pil_img = barcode_pil_img.resize(
        (label_size_px[0], barcode_height),#int(barcode_height * scale_factor)),
        Image.LANCZOS,  # Verwende direkt LANCZOS für die Skalierung
    )

    # Positioniere den Barcode unten auf dem Label
    barcode_x = 0
    barcode_y = label_size_px[1] - barcode_pil_img.size[1] + 15
    label_image.paste(barcode_pil_img, (barcode_x, barcode_y))

    return label_image

def image_to_pdf(label_image, output_file="label", num_copies = 1):
    """
    Speichert das Label als PDF.
    """
    if num_copies > 1:
        tmp_file = "/tmp/label_temp.pdf"
        label_image.save(tmp_file, "PDF")
        
        output = PdfWriter()
        label = PdfReader(open(tmp_file, "rb"))
        for _ in range(num_copies):
            output.add_page(label.pages[0])
        outputStream = open(f"{output_file}.pdf", "wb")
        output.write(outputStream)
        outputStream.close()
        os.remove(tmp_file)
        
    else: label_image.save(f"{output_file}.pdf", "PDF")

@get('/api/preview/text')
@post('/api/preview/text')
def get_preview_image():
    context = get_label_context(request)
    im = create_label_im(**context)
    return_format = request.query.get('return_format', 'png')
    if return_format == 'base64':
        import base64
        response.set_header('Content-type', 'text/plain')
        return base64.b64encode(image_to_png_bytes(im))
    else:
        response.set_header('Content-type', 'image/png')
        return image_to_png_bytes(im)

def image_to_png_bytes(im):
    image_buffer = BytesIO()
    im.save(image_buffer, format="PNG")
    image_buffer.seek(0)
    return image_buffer.read()

@post('/api/print/grocy')
@get('/api/print/grocy')
def print_grocy():
    """
    API endpoint to consume the grocy label webhook.

    returns; JSON
    """

    return_dict = {'success' : False }

    try:
        context = get_label_context(request)
    except LookupError as e:
        return_dict['error'] = e.msg
        return return_dict

    if context['product'] is None:
        return_dict['error'] = 'Please provide the product for the label'
        return return_dict
    
    if context['duedate'] is None:
        context['duedate'] = ""
    
    print(context)
    
    #-- Add Code to create label and print it
    im = create_label_grocy(context)
    image_to_pdf(im, context['grocycode'])
    cups_connection = cups.Connection()
    #cups_connection.printFile(CONFIG['PRINTER']['CUPS_PRINTER'], f"{context['grocycode']}.pdf", "DYMO Label", {'choice': 'auto-fit'})
    #-- Add Code to create label and print it
    

    if not DEBUG:
        try:
            be = BACKEND_CLASS(CONFIG['PRINTER']['PRINTER'])
            be.write(qlr.data)
            be.dispose()
            del be
        except Exception as e:
            return_dict['message'] = str(e)
            logger.warning('Exception happened: %s', e)
            return return_dict

    return_dict['success'] = True
    if DEBUG: return_dict['data'] = str(qlr.data)
    return return_dict

@post('/api/print/text')
@get('/api/print/text')
def print_text():
    """
    API to print a label

    returns: JSON

    Ideas for additional URL parameters:
    - alignment
    """

    return_dict = {'success': False}

    try:
        context = get_label_context(request)
    except LookupError as e:
        return_dict['error'] = e.msg
        return return_dict

    if context['text'] is None:
        return_dict['error'] = 'Please provide the text for the label'
        return return_dict

    im = create_label_im(**context)
    if DEBUG: im.save('sample-out.png')

    if context['kind'] == ENDLESS_LABEL:
        rotate = 0 if context['orientation'] == 'standard' else 90
    elif context['kind'] in (ROUND_DIE_CUT_LABEL, DIE_CUT_LABEL):
        rotate = 'auto'

    qlr = BrotherQLRaster(CONFIG['PRINTER']['MODEL'])
    red = False
    if 'red' in context['label_size']:
        red = True
    create_label(qlr, im, context['label_size'], red=red, threshold=context['threshold'], cut=True, rotate=rotate)

    if not DEBUG:
        try:
            be = BACKEND_CLASS(CONFIG['PRINTER']['PRINTER'])
            be.write(qlr.data)
            be.dispose()
            del be
        except Exception as e:
            return_dict['message'] = str(e)
            logger.warning('Exception happened: %s', e)
            return return_dict

    return_dict['success'] = True
    if DEBUG: return_dict['data'] = str(qlr.data)
    return return_dict

def main():
    global DEBUG, FONTS, BACKEND_CLASS, CONFIG
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', default=False)
    parser.add_argument('--loglevel', type=lambda x: getattr(logging, x.upper()), default=False)
    parser.add_argument('--font-folder', default=False, help='folder for additional .ttf/.otf fonts')
    parser.add_argument('--default-label-size', default=False, help='Label size inserted in your printer. Defaults to 57x32.')
    parser.add_argument('--default-orientation', default=False, choices=('standard', 'rotated'), help='Label orientation, defaults to "standard". To turn your text by 90°, state "rotated".')
    parser.add_argument('--model', default=False, choices=models, help='The model of your printer (default: QL-500)')
    parser.add_argument('--cups', default=False, help='The CUPS name of your printer')
    parser.add_argument('printer',  nargs='?', default=False, help='String descriptor for the printer to use (like tcp://192.168.0.23:9100 or file:///dev/usb/lp0)')
    args = parser.parse_args()

    if args.printer:
        CONFIG['PRINTER']['PRINTER'] = args.printer

    if args.port:
        PORT = args.port
    else:
        PORT = CONFIG['SERVER']['PORT']

    if args.loglevel:
        LOGLEVEL = args.loglevel
    else:
        LOGLEVEL = CONFIG['SERVER']['LOGLEVEL']

    if LOGLEVEL == 'DEBUG':
        DEBUG = True
    else:
        DEBUG = False

    if args.model:
        CONFIG['PRINTER']['MODEL'] = args.model
        
    if args.cups:
        CONFIG['PRINTER']['CUPS_PRINTER'] = args.cups

    if args.default_label_size:
        CONFIG['LABEL']['DEFAULT_SIZE'] = args.default_label_size

    if args.default_orientation:
        CONFIG['LABEL']['DEFAULT_ORIENTATION'] = args.default_orientation

    if args.font_folder:
        ADDITIONAL_FONT_FOLDER = args.font_folder
    else:
        ADDITIONAL_FONT_FOLDER = CONFIG['SERVER']['ADDITIONAL_FONT_FOLDER']


    logging.basicConfig(level=LOGLEVEL)

    try:
        selected_backend = guess_backend(CONFIG['PRINTER']['PRINTER'])
    except ValueError:
        parser.error("Couln't guess the backend to use from the printer string descriptor")
    BACKEND_CLASS = backend_factory(selected_backend)['backend_class']

    if CONFIG['LABEL']['DEFAULT_SIZE'] not in label_sizes:
        parser.error("Invalid --default-label-size. Please choose on of the following:\n:" + " ".join(label_sizes))

    FONTS = get_fonts()
    if ADDITIONAL_FONT_FOLDER:
        FONTS.update(get_fonts(ADDITIONAL_FONT_FOLDER))

    if not FONTS:
        sys.stderr.write("Not a single font was found on your system. Please install some or use the \"--font-folder\" argument.\n")
        sys.exit(2)

    for font in CONFIG['LABEL']['DEFAULT_FONTS']:
        try:
            FONTS[font['family']][font['style']]
            CONFIG['LABEL']['DEFAULT_FONTS'] = font
            logger.debug("Selected the following default font: {}".format(font))
            break
        except: pass
    if CONFIG['LABEL']['DEFAULT_FONTS'] is None:
        sys.stderr.write('Could not find any of the default fonts. Choosing a random one.\n')
        family =  random.choice(list(FONTS.keys()))
        style =   random.choice(list(FONTS[family].keys()))
        CONFIG['LABEL']['DEFAULT_FONTS'] = {'family': family, 'style': style}
        sys.stderr.write('The default font is now set to: {family} ({style})\n'.format(**CONFIG['LABEL']['DEFAULT_FONTS']))

    run(host=CONFIG['SERVER']['HOST'], port=PORT, debug=DEBUG)

if __name__ == "__main__":
    main()

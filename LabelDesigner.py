#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""
This is a web service to print labels on Dymo 450 label printers.
"""

import sys, logging, random, json, argparse
from io import BytesIO
import os
from datetime import date
import re

from bottle import run, route, get, post, response, request, jinja2_view as view, static_file, redirect
from PIL import Image, ImageDraw, ImageFont

from barcode import Code128
from barcode.writer import ImageWriter
from PyPDF2 import PdfReader, PdfWriter
import cups

from pygrocy import Grocy

from font_helpers import get_fonts

logger = logging.getLogger(__name__)

LABEL_SIZES = [
    ('57x32', '57mm x 32mm'),
    ('54x25', '54mm x 25mm')
]

try:
    with open('config.json', encoding='utf-8') as fh:
        CONFIG = json.load(fh)
except FileNotFoundError as e:
    with open('config.example.json', encoding='utf-8') as fh:
        CONFIG = json.load(fh)

if CONFIG['GROCY']['ENABLE']:
    grocy = Grocy(CONFIG['GROCY']['URI'], CONFIG['GROCY']['API_KEY'], port = CONFIG['GROCY']['PORT'], verify_ssl = CONFIG['GROCY']['SSL'])

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
    
    print_alias =    False if int(d.get('print_alias', CONFIG['GROCY']['PRINT_ALIAS'])) == 0       else True
    print_due_date = False if int(d.get('print_due_date', CONFIG['GROCY']['PRINT_DUE_DATE'])) == 0 else True
    print_today =    False if int(d.get('print_today', CONFIG['GROCY']['PRINT_TODAY'])) == 0       else True
    print_date =     False if int(d.get('print_date', CONFIG['GROCY']['PRINT_DATE'])) == 0         else True
    
    if DEBUG: 
        for key in d: print(key+":"+d.get(key))
        
    context = {
      'text':           d.get('text', None),
      'grocycode':      d.get('grocycode', None),
      'product':        d.get('product', None),
      'print_alias':    print_alias,
      'due_date':       d.get('due_date', None),
      'print_due_date': print_due_date,
      'print_today':    print_today,
      'print_date':     print_date,
      'alias_userfield':d.get('alias_userfield', 'kurzname'),
      'width':          int(d.get('label_size', CONFIG['LABEL']['DEFAULT_SIZE']).rpartition('x')[0].rstrip()),
      'height':         int(d.get('label_size', CONFIG['LABEL']['DEFAULT_SIZE']).rpartition('x')[2].rstrip()),
      'dpi':            d.get('dpi',300),
      'font_family':    font_family,
      'font_style':     font_style,
      'font_size':      int(d.get('font_size', 70)),
      'margin_top':     float(d.get('margin_top',    0)),
      'margin_bottom':  float(d.get('margin_bottom', 0)),
      'margin_left':    float(d.get('margin_left',   0)),
      'margin_right':   float(d.get('margin_right',  0)),
      'line_spacing':   float(d.get('line_spacing', 8)),
      'align':          d.get('align', 'center')
    }
    
    context['fill_color'] = (0, 0, 0)
    
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

def draw_multiline_text(img, text, font, kwargs, offset):
    width = img.size[0] - offset[1]  # Adjust for padding if needed
    draw = ImageDraw.Draw(img)

    def tokenize(text):
        # Split text into tokens where everything in parentheses is treated as one word
        pattern = r'\([^)]*\)|\S+'
        return re.findall(pattern, text)

    def break_fix(text, width, font, draw):
        tokens = tokenize(text)
        line = []
        for token in tokens:
            test_line = ' '.join(line + [token])
            x, y, w, h = draw.textbbox((0, 0), text=test_line, font=font)
            if w <= width:
                line.append(token)
            else:
                yield ' '.join(line)
                line = [token]
        if line:
            yield ' '.join(line)

    lines = list(break_fix(text, width, font, draw))
    line_heights = [draw.textbbox((0, 0), text=line, font=font)[3] for line in lines]
    total_height = sum(line_heights) + (len(line_heights) - 1) * kwargs.get('line_spacing', 0)

    if total_height > img.size[1]:
        raise ValueError("Text doesn't fit within the image dimensions")

    # Start drawing from the top-left corner
    y = kwargs.get('margin_top', 0)

    align = kwargs.get('align', 'left')
    for i, line in enumerate(lines):
        x, _, line_width, _ = draw.textbbox((0, 0), text=line, font=font)
        if align == 'left':
            x = kwargs.get('margin_left', 0)
        elif align == 'center':
            x = (img.size[0] - line_width) // 2
        elif align == 'right':
            x = img.size[0] - line_width - kwargs.get('margin_right', 0)
        else:
            raise ValueError("Invalid align value. Choose from 'left', 'center', or 'right'.")

        draw.text((x, y), line, font=font, fill=kwargs.get('fill_color', 'black'))
        y += line_heights[i] + kwargs.get('line_spacing', 0)

    return img



def create_label_im(text, **kwargs):
    im_font = ImageFont.truetype(kwargs['font_path'], kwargs['font_size'])
    im = Image.new('L', (20, 20), 'white')
    draw = ImageDraw.Draw(im)

    lines = []
    for line in text.split('\n'):
        if line == '': line = ' '
        lines.append(line)
    text = '\n'.join(lines)

    textsize = draw.multiline_textbbox(xy=(0,0),text=text, font=im_font)
    width, height = kwargs['width'], kwargs['height']
    im = Image.new('RGB', (width*10, height*10), 'white')
    draw = ImageDraw.Draw(im)
    
    vertical_offset  = (height - textsize[1])//2
    vertical_offset += (kwargs['margin_top'] - kwargs['margin_bottom'])//2
    horizontal_offset = max((width - textsize[2])//2, 0)
    horizontal_offset = kwargs['margin_left'] - kwargs['margin_right']
    offset = horizontal_offset, vertical_offset
    
    draw_multiline_text(im, text, im_font, kwargs, offset)

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

    barcode_data = kwargs['grocycode']
    label_size_mm = (kwargs['width'],kwargs['height'])
    dpi = kwargs['dpi']
    
    # Berechnung der Pixel basierend auf DPI
    label_size_px = (int(label_size_mm[0] / 25.4 * dpi), int(label_size_mm[1] / 25.4 * dpi))
    width, height = label_size_mm
    
    # Erstelle das Label-Bild
    label_image = Image.new("RGB", label_size_px, "white")
    draw = ImageDraw.Draw(label_image)

    # Schriftart laden (Systemschrift oder Standardschrift)
    try:
        im_font = ImageFont.truetype(kwargs['font_path'], kwargs['font_size'])
    except IOError:
        im_font = ImageFont.load_default()
    
    textsize = draw.multiline_textbbox(xy=(0,0),text=kwargs['product'], font=im_font)
    
    vertical_offset  = (height - textsize[1])//2
    vertical_offset += (kwargs['margin_top'] - kwargs['margin_bottom'])//2
    horizontal_offset = max((width - textsize[2])//2, 0)
    horizontal_offset = kwargs['margin_left'] - kwargs['margin_right']
    offset = horizontal_offset, vertical_offset

    draw_multiline_text(label_image, f"{kwargs['product']}\n{kwargs['due_date']}", im_font, kwargs, offset)

    # Barcode hinzufügen
    barcode_class = Code128(barcode_data, writer=ImageWriter())
    barcode_options = {
        "module_width": 0.4,  # Breite eines Moduls in mm
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
    
    crop = 15
    barcode_pil_img = barcode_pil_img.crop((0, crop, barcode_width, barcode_height-crop))
    barcode_pil_img = barcode_pil_img.resize(
        (label_size_px[0], label_size_px[1]//2),#int(barcode_height * scale_factor)),
        Image.LANCZOS,  # Verwende direkt LANCZOS für die Skalierung
    )
    
    if DEBUG: barcode_pil_img.save(f"{barcode_data}.png")

    # Positioniere den Barcode unten auf dem Label
    barcode_x = 0
    barcode_y = label_size_px[1] - barcode_pil_img.size[1]
    barcode_y += 15 if kwargs['width']==57 and kwargs['height']==32 else 0
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
    
    if context['print_alias']:
        product = grocy.product_by_barcode(context['grocycode'])
        alias_name = grocy.get_userfields("products",product.id).get(context['alias_userfield'])
        if alias_name is not None and 0 < len(alias_name) < len(context["product"]):
            context["product"] = alias_name
    
    # if context['width']==57 and context['height']==32:
    if context['due_date'] is None:
        context['due_date'] = f"({date.today()})" if context['print_date'] else ""
    else: context['due_date'] = f"({context['due_date']})" if context['print_due_date'] else f"({date.today()})" if context['print_today'] else ""
    # else: context['due_date'] = ''
    
    if DEBUG: print(context)
    
    im = create_label_grocy(context)
    image_to_pdf(im, context['grocycode'])    

    if not DEBUG:
        try:
            cups_connection = cups.Connection()
            cups_connection.printFile(CONFIG['PRINTER']['PRINTER'], f"{context['grocycode']}.pdf", "DYMO Label", {'choice': 'auto-fit'})
            os.remove(f"{context['grocycode']}.pdf")
            del cups_connection
        except Exception as e:
            return_dict['message'] = str(e)
            logger.warning('Exception happened: %s', e)
            return return_dict

    return_dict['success'] = True
    if DEBUG: return_dict['data'] = f"{context['grocycode']}.pdf"
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
    
    if not DEBUG:
        try:
            cups_connection = cups.Connection()
            cups_connection.printFile(CONFIG['PRINTER']['PRINTER'], im, "DYMO Label", {'choice': 'auto-fit'})
            del cups_connection
        except Exception as e:
            return_dict['message'] = str(e)
            logger.warning('Exception happened: %s', e)
            return return_dict

    return_dict['success'] = True
    return return_dict

def main():
    global DEBUG, FONTS, BACKEND_CLASS, CONFIG
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', default=False)
    parser.add_argument('--loglevel', type=lambda x: getattr(logging, x.upper()), default=False)
    parser.add_argument('--font-folder', default=False, help='folder for additional .ttf/.otf fonts')
    parser.add_argument('--default-label-size', default=False, help='Label size inserted in your printer. Defaults to 57x32.')
    parser.add_argument('--default-orientation', default=False, choices=('standard', 'rotated'), help='Label orientation, defaults to "standard". To turn your text by 90°, state "rotated".')
    parser.add_argument('--grocy', default=False, help="URI of your grocy instance")
    parser.add_argument('--api', default=False, help="Grocy API-Key")
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

    if args.default_label_size:
        CONFIG['LABEL']['DEFAULT_SIZE'] = args.default_label_size

    if args.default_orientation:
        CONFIG['LABEL']['DEFAULT_ORIENTATION'] = args.default_orientation

    if args.font_folder:
        ADDITIONAL_FONT_FOLDER = args.font_folder
    else:
        ADDITIONAL_FONT_FOLDER = CONFIG['SERVER']['ADDITIONAL_FONT_FOLDER']
        
    logging.basicConfig(level=LOGLEVEL)

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

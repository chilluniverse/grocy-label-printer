from PIL import Image, ImageDraw, ImageFont
from barcode import Code128
from barcode.writer import ImageWriter
from io import BytesIO
from PyPDF2 import PdfReader, PdfWriter
import cups
import os

def create_label(text_lines, barcode_data, label_size_mm=(57, 32), dpi=300):
    """
    Erstellt ein Label mit Text und einem Code-128 Barcode.
    
    :param text_lines: Liste mit Textzeilen (max. 2 Zeilen)
    :param barcode_data: Daten für den Code-128 Barcode
    :param label_size_mm: Maße des Labels in Millimetern (Breite, Höhe)
    :param dpi: Druckauflösung (dots per inch)
    :return: PIL-Image des Labels
    """
    # Berechnung der Pixel basierend auf DPI
    label_size_px = (int(label_size_mm[0] / 25.4 * dpi), int(label_size_mm[1] / 25.4 * dpi))

    # Erstelle das Label-Bild
    label_image = Image.new("RGB", label_size_px, "white")
    draw = ImageDraw.Draw(label_image)

    desired_font_height_mm = 6
    # Schriftart laden (Systemschrift oder Standardschrift)
    try:
        font_size = int(desired_font_height_mm / 25.4 * dpi)  # Schriftgröße: 8 mm (entspricht ~22.7 pt bei 300 DPI)
        font = ImageFont.truetype("/usr/share/fonts/truetype/bahnschrift/BAHNSCHRIFT.TTF", font_size)
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
        "module_height": int(15 / 25.4 * dpi)/20,  # Höhe des Barcodes: 15 mm
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
        (label_size_px[0], int(barcode_height * scale_factor)),
        Image.LANCZOS,  # Verwende direkt LANCZOS für die Skalierung
    )

    # Positioniere den Barcode unten auf dem Label
    barcode_x = 0
    barcode_y = label_size_px[1] - barcode_pil_img.size[1] + 15
    label_image.paste(barcode_pil_img, (barcode_x, barcode_y))

    return label_image


def save_as_pdf(label_image, output_file="label", num_copies = 1):
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


def print_label(printer_name, output_file="label.pdf"):
    """Druckt das Label mit CUPS."""
    cups_connection = cups.Connection()
    cups_connection.printFile(printer_name, f"{output_file}.pdf", "DYMO Label", {'choice': 'auto-fit'})


def main():
    # Benutzerdefinierte Daten
    text_lines = ["Ofengemüse", "(29.12.2024)"]
    barcode_data = "grcy:p:93"
    printer_name = "DYMO_LabelWriter_450_LabelWrite"
    num_copies = 2

    print("Erstelle Label...")
    label_image = create_label(text_lines, barcode_data)

    save_as_pdf(label_image, barcode_data, num_copies)
    print(f"Label gespeichert als {barcode_data}.pdf!")
    
    #print_label(printer_name, barcode_data)
    print(f"Label {barcode_data}.pdf an Drucker gesendet!")


if __name__ == "__main__":
    main()

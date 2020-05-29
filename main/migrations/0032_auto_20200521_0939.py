# Generated by Django 3.0.3 on 2020-05-21 09:39

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('main', '0031_auto_20200521_0754'),
    ]

    operations = [
        migrations.AlterModelOptions(
            name='slpaddress',
            options={'verbose_name': 'SLP Address', 'verbose_name_plural': 'SLP Addresses'},
        ),
        migrations.CreateModel(
            name='BchAddress',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('address', models.CharField(max_length=200, unique=True)),
                ('transactions', models.ManyToManyField(blank=True, related_name='bchaddress', to='main.Transaction')),
            ],
            options={
                'verbose_name': 'BCH Address',
                'verbose_name_plural': 'BCH Addresses',
            },
        ),
    ]